"""Which algorithm should run this target on this host — inventory-blind.

``plan_strategy`` answers a different question: given the binaries that happen to
be installed *right now*, what can run. That is the correct question for ``plan``
and ``batch``, and the wrong one for ``auto``, whose whole job is to install
whatever the answer turns out to be.

Deciding from installed inventory also inverts the dependency (docs/ARCHITECTURE.md
§5): installing RCKangaroo once moved puzzle #160 from the CPU queue to the GPU
queue, because the resource class was read off ``available_engines()`` instead of
following from the algorithm family. So this module derives the choice from
``(target, host capability)`` alone and reports the build requirement separately.

A missing capability yields ``blocked`` with an explicit remedy rather than a
quiet relocation to another resource class.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.search import DEFAULT_CHUNK_SIZE, MAX_SEQUENTIAL_KEYS
from btc_puzzle_lab.strategy import SEQUENTIAL_BITS, HostProfile
from btc_puzzle_lab.toolchain import cuda_available

ResourceClass = Literal["cpu", "gpu"]

# Kangaroo-class solvers need a range wide enough to be worth their setup; both
# upstreams also refuse to run below this.
PUBKEY_MIN_BITS = 32

# Distinguished-point bits for kangaroo-class engines when no calibration table
# exists. The engine default of 16 grows the DP table ~35 GB/h, which OOM-kills a
# 116 GB container in about 3.4 hours and discards every accumulated point with it.
# Across dp 23..32 the extra algorithmic work is under 0.003% (ARCHITECTURE.md §8),
# so the largest survivable value is the conservative pick, not a tuning risk.
SAFE_DP = 30

# Engines that ship inside this package and need no clone/build step.
BUILT_IN = frozenset({"sequential", "window", "inject-known"})

# GPU engine -> the CPU engine solving the same problem, for an explicit downgrade.
_CPU_ALTERNATIVE = {
    "rckangaroo": "kangaroo",
    "bitcrack": "keyhunt",
}


@dataclass(frozen=True)
class EngineChoice:
    """One engine decision, plus everything ``auto`` needs to act on it."""

    engine: str
    resource: ResourceClass
    reason: str
    needs_install: bool
    dp: int | None = None
    blocked: str | None = None
    remedy: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked is None

    def format(self) -> str:
        if not self.ok:
            lines = [f"blocked: {self.blocked}"]
            if self.remedy:
                lines.append(f"  remedy: {self.remedy}")
            return "\n".join(lines)
        bits = [f"engine={self.engine}", f"resource={self.resource}"]
        if self.dp is not None:
            bits.append(f"dp={self.dp}")
        bits.append("build=required" if self.needs_install else "build=not needed")
        return f"{' '.join(bits)} — {self.reason}"


def cpu_alternative(engine: str) -> str | None:
    """The CPU engine that solves the same problem shape, if there is one."""
    return _CPU_ALTERNATIVE.get(engine)


def _blocked_on_cuda(engine: str, host: HostProfile) -> EngineChoice:
    card = f" ({host.gpu_name})" if host.gpu_name else ""
    fallback = cpu_alternative(engine)
    remedy = (
        "install the CUDA toolkit so nvcc is on PATH (Debian/Ubuntu: the NVIDIA "
        "cuda-toolkit package), then re-run"
    )
    if fallback:
        remedy += f"; or pass --allow-cpu-fallback to run {fallback} on the CPU instead"
    return EngineChoice(
        engine=engine,
        resource="gpu",
        reason=f"{engine} is the right engine for this target",
        needs_install=True,
        blocked=f"GPU detected{card} but no CUDA toolkit, so {engine} cannot be built",
        remedy=remedy,
    )


def recommend_engine(
    puzzle: Puzzle,
    host: HostProfile,
    *,
    cuda: bool | None = None,
    allow_cpu_fallback: bool = False,
) -> EngineChoice:
    """Pick the engine for ``puzzle`` on ``host``, ignoring what is installed.

    ``cuda`` is the one capability that cannot be read off ``host``; it defaults to
    probing for ``nvcc`` and is injectable so the policy stays testable without a
    GPU box.
    """
    has_cuda = cuda_available() if cuda is None else cuda
    has_pubkey = bool(puzzle.pubkey_compressed_hex)
    span = puzzle.range_end - puzzle.range_start + 1

    # 1. Ranges the bundled scanner can finish outright need no toolchain at all.
    if puzzle.bits <= SEQUENTIAL_BITS and span <= MAX_SEQUENTIAL_KEYS:
        return EngineChoice(
            engine="sequential",
            resource="cpu",
            reason=(
                f"{puzzle.bits}-bit range is {span:,} keys — the built-in scanner "
                "covers it without an external solver"
            ),
            needs_install=False,
        )

    # 2. A known public key buys a square-root speedup, so kangaroo-class engines
    #    beat any address brute force regardless of how fast the card is.
    if has_pubkey and puzzle.bits >= PUBKEY_MIN_BITS:
        if host.gpu:
            if not has_cuda:
                if not allow_cpu_fallback:
                    return _blocked_on_cuda("rckangaroo", host)
                return EngineChoice(
                    engine="kangaroo",
                    resource="cpu",
                    reason=(
                        f"{puzzle.bits}-bit pubkey target; GPU present but no CUDA "
                        "toolkit, downgraded to the CPU kangaroo by --allow-cpu-fallback"
                    ),
                    needs_install=True,
                )
            return EngineChoice(
                engine="rckangaroo",
                resource="gpu",
                reason=(
                    f"{puzzle.bits}-bit target with a known pubkey — GPU kangaroo is "
                    "the fastest engine here"
                ),
                needs_install=True,
                dp=SAFE_DP,
            )
        return EngineChoice(
            engine="kangaroo",
            resource="cpu",
            reason=(
                f"{puzzle.bits}-bit target with a known pubkey — Pollard kangaroo on "
                "the CPU (no GPU on this host)"
            ),
            needs_install=True,
        )

    # 3. Address-only target: brute force the range.
    if host.gpu:
        if has_cuda:
            return EngineChoice(
                engine="bitcrack",
                resource="gpu",
                reason=(
                    f"{puzzle.bits}-bit address-only target — GPU brute force "
                    f"({host.gpu_name or 'CUDA device'})"
                ),
                needs_install=True,
            )
        if not allow_cpu_fallback:
            return _blocked_on_cuda("bitcrack", host)

    return EngineChoice(
        engine="keyhunt",
        resource="cpu",
        reason=(
            f"{puzzle.bits}-bit address-only target — keyhunt is the CPU address "
            "search for this host"
        ),
        needs_install=True,
    )


def run_kwargs_for(choice: EngineChoice, host: HostProfile) -> dict[str, int | bool | str | None]:
    """Search knobs implied by a choice, for callers driving ``run_puzzle`` directly."""
    threads = min(max(1, host.cpus), 8)
    kwargs: dict[str, int | bool | str | None] = {
        "engine": choice.engine,
        "threads": threads,
        "workers": 1,
        "chunk_size": DEFAULT_CHUNK_SIZE,
    }
    if choice.dp is not None:
        kwargs["dp"] = choice.dp
    return kwargs
