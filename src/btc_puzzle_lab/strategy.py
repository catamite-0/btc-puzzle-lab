"""Environment-adaptive search strategy.

Probe the host once (CPU / RAM / GPU / installed engines), classify a tier,
then return one algorithm plan per puzzle for ``run --auto`` / ``plan`` / ``batch``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.engines import available_engines
from btc_puzzle_lab.search import DEFAULT_CHUNK_SIZE, MAX_SEQUENTIAL_KEYS

SEQUENTIAL_BITS = 20
LOW_MEM_MB = 2048
STANDARD_MEM_MB = 8192

# Distinguished-point bits for kangaroo-class engines. Upstream defaults to 16,
# which grows the DP table ~35 GB/h and OOM-kills a 116 GB container in ~3.4 h,
# discarding every accumulated point. Across dp 23..32 the extra work is under
# 0.003% (ARCHITECTURE.md §8), so the largest survivable value is the default.
SAFE_DP = 30
PUBKEY_MIN_BITS = 32

HostTier = Literal["constrained", "standard", "gpu", "compute"]
ResourceClass = Literal["cpu", "gpu"]

GPU_ENGINES = frozenset({"bitcrack", "rckangaroo"})
KANGAROO_ENGINES = frozenset({"kangaroo", "rckangaroo"})


@dataclass(frozen=True)
class HostProfile:
    cpus: int
    mem_mb: int
    engines: frozenset[str]
    gpu: bool = False
    gpu_name: str = ""
    disk_free_mb: int | None = None
    tier: HostTier = "constrained"
    overrides: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpus": self.cpus,
            "mem_mb": self.mem_mb,
            "engines": sorted(self.engines),
            "gpu": self.gpu,
            "gpu_name": self.gpu_name,
            "disk_free_mb": self.disk_free_mb,
            "tier": self.tier,
            "overrides": list(self.overrides),
        }


@dataclass(frozen=True)
class StrategyPlan:
    engine: str
    reason: str
    workers: int = 1
    threads: int = 2
    dp: int = SAFE_DP
    coverage: bool = False
    chunk_size: int = DEFAULT_CHUNK_SIZE
    order: str = "sequential"
    seed: int | None = None
    window: int = 1_000_000
    max_chunks: int | None = None
    tier: HostTier = "constrained"

    @property
    def resource(self) -> ResourceClass:
        """Scarce accelerator class this plan should occupy on one machine."""
        return "gpu" if self.engine in GPU_ENGINES else "cpu"

    def format(self) -> str:
        bits = [
            f"tier={self.tier}",
            f"resource={self.resource}",
            f"engine={self.engine}",
            f"workers={self.workers}",
            f"coverage={self.coverage}",
        ]
        if self.coverage:
            bits += [f"chunk_size={self.chunk_size}", f"order={self.order}"]
            if self.max_chunks is not None:
                bits.append(f"max_chunks={self.max_chunks}")
        if self.engine == "window":
            bits.append(f"window={self.window}")
        if self.engine == "keyhunt":
            bits.append(f"threads={self.threads}")
        if self.engine in KANGAROO_ENGINES:
            bits += [f"threads={self.threads}", f"dp={self.dp}"]
        return f"{' '.join(bits)} — {self.reason}"


_CGROUP_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),  # cgroup v2
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),  # cgroup v1
)


def cgroup_limit_mb() -> int | None:
    """Memory this process may actually use, when a cgroup caps it."""
    for path in _CGROUP_LIMIT_PATHS:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            limit = int(raw)
        except ValueError:
            continue
        # v1 reports a huge sentinel rather than an explicit "unlimited".
        if limit <= 0 or limit > (1 << 62):
            return None
        return limit // (1024 * 1024)
    return None


def _mem_mb() -> int:
    """Usable memory, not the machine's.

    /proc/meminfo reports the host inside a container: this box advertises
    377 GB while the cgroup caps it at 116 GB. Planning against the larger
    number is how a growing DP table walks into the OOM killer.
    """
    total = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) // 1024
                break
    except (OSError, ValueError, IndexError):
        total = None
    limit = cgroup_limit_mb()
    candidates = [value for value in (total, limit) if value]
    return min(candidates) if candidates else LOW_MEM_MB


def _disk_free_mb(path: Path | None = None) -> int | None:
    try:
        usage = shutil.disk_usage(str(path or Path.cwd()))
        return usage.free // (1024 * 1024)
    except OSError:
        return None


def _probe_gpu() -> tuple[bool, str]:
    """Best-effort NVIDIA GPU probe (optional)."""
    if shutil.which("nvidia-smi") is None:
        return False, ""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if proc.returncode != 0:
        return False, ""
    names = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if not names:
        return False, ""
    return True, names[0]


def classify_tier(
    *,
    cpus: int,
    mem_mb: int,
    gpu: bool,
    engines: frozenset[str],
) -> HostTier:
    has_gpu_solver = bool(engines.intersection({"bitcrack", "rckangaroo"}))
    if gpu or has_gpu_solver:
        return "gpu"
    if mem_mb >= STANDARD_MEM_MB and cpus >= 4:
        return "compute"
    if mem_mb >= LOW_MEM_MB and cpus >= 2:
        return "standard"
    return "constrained"


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return int(raw)


def probe_host() -> HostProfile:
    overrides: list[str] = []
    cpus = max(1, os.cpu_count() or 1)
    env_cpus = _env_int("BTC_PUZZLE_LAB_CPUS")
    if env_cpus is not None and env_cpus >= 1:
        cpus = env_cpus
        overrides.append("BTC_PUZZLE_LAB_CPUS")

    mem_mb = _mem_mb()
    env_mem = _env_int("BTC_PUZZLE_LAB_MEM_MB")
    if env_mem is not None and env_mem >= 256:
        mem_mb = env_mem
        overrides.append("BTC_PUZZLE_LAB_MEM_MB")

    force_gpu = os.environ.get("BTC_PUZZLE_LAB_GPU", "").strip().lower()
    if force_gpu in {"1", "true", "yes", "on"}:
        gpu, gpu_name = True, "forced"
        overrides.append("BTC_PUZZLE_LAB_GPU")
    elif force_gpu in {"0", "false", "no", "off"}:
        gpu, gpu_name = False, ""
        overrides.append("BTC_PUZZLE_LAB_GPU")
    else:
        gpu, gpu_name = _probe_gpu()

    engines = frozenset(available_engines())
    tier = classify_tier(cpus=cpus, mem_mb=mem_mb, gpu=gpu, engines=engines)
    return HostProfile(
        cpus=cpus,
        mem_mb=mem_mb,
        engines=engines,
        gpu=gpu,
        gpu_name=gpu_name,
        disk_free_mb=_disk_free_mb(),
        tier=tier,
        overrides=tuple(overrides),
    )


def _has_pubkey(puzzle: Puzzle) -> bool:
    return bool(puzzle.pubkey_compressed_hex)


def _resource_knobs(profile: HostProfile) -> dict[str, int | None]:
    """Map host tier → workers/threads/chunk/window/max_chunks."""
    if profile.tier == "constrained":
        return {
            "workers": 1,
            "threads": min(max(1, profile.cpus), 2),
            "chunk": 16_384,
            "window": 250_000,
            "max_chunks": 2,
        }
    if profile.tier == "standard":
        return {
            "workers": min(2, profile.cpus),
            "threads": min(max(1, profile.cpus), 4),
            "chunk": DEFAULT_CHUNK_SIZE,
            "window": 1_000_000,
            "max_chunks": 4,
        }
    if profile.tier == "gpu":
        return {
            "workers": min(2, profile.cpus),
            "threads": min(max(1, profile.cpus), 8),
            "chunk": DEFAULT_CHUNK_SIZE,
            "window": 2_000_000,
            "max_chunks": 8,
        }
    # compute
    return {
        "workers": min(4, profile.cpus),
        "threads": min(max(1, profile.cpus), 8),
        "chunk": DEFAULT_CHUNK_SIZE * 2,
        "window": 4_000_000,
        "max_chunks": 16,
    }


def plan_strategy(puzzle: Puzzle, host: HostProfile | None = None) -> StrategyPlan:
    """Choose one engine/config for this puzzle on this host."""
    probed = host or probe_host()
    # Always recompute tier from resources so callers can pass partial HostProfile.
    tier = classify_tier(
        cpus=probed.cpus,
        mem_mb=probed.mem_mb,
        gpu=probed.gpu,
        engines=probed.engines,
    )
    profile = HostProfile(
        cpus=probed.cpus,
        mem_mb=probed.mem_mb,
        engines=probed.engines,
        gpu=probed.gpu,
        gpu_name=probed.gpu_name,
        disk_free_mb=probed.disk_free_mb,
        tier=tier,
        overrides=probed.overrides,
    )
    knobs = _resource_knobs(profile)
    workers = int(knobs["workers"] or 1)
    threads = int(knobs["threads"] or 1)
    # Tier knobs cap threads at 8; let operators feed a big CPU box explicitly.
    env_threads = _env_int("BTC_PUZZLE_LAB_THREADS")
    if env_threads and env_threads > 0:
        threads = env_threads
    chunk = int(knobs["chunk"] or DEFAULT_CHUNK_SIZE)
    window = int(knobs["window"] or 1_000_000)
    max_chunks = knobs["max_chunks"]
    dp = SAFE_DP
    env_dp = _env_int("BTC_PUZZLE_LAB_DP")
    if env_dp and env_dp > 0:
        dp = env_dp
    # Operator override: pin the engine (and therefore the cpu/gpu slot) instead of
    # letting availability decide. batch._job_status still blocks impossible picks.
    forced = os.environ.get("BTC_PUZZLE_LAB_ENGINE", "").strip().lower()
    if forced:
        return StrategyPlan(
            engine=forced,
            workers=workers,
            threads=threads,
            dp=dp,
            window=window,
            tier=profile.tier,
            reason=f"tier={profile.tier}: engine pinned by BTC_PUZZLE_LAB_ENGINE={forced}",
        )

    range_size = puzzle.range_end - puzzle.range_start + 1
    installed = profile.engines

    # Pubkey-class solvers first when bits are large enough.
    if _has_pubkey(puzzle) and puzzle.bits >= PUBKEY_MIN_BITS:
        if "rckangaroo" in installed:
            return StrategyPlan(
                engine="rckangaroo",
                threads=threads,
                dp=dp,
                tier=profile.tier,
                reason=(
                    f"tier={profile.tier}: RCKangaroo available for "
                    f"{puzzle.bits}-bit pubkey search"
                ),
            )
        if "kangaroo" in installed:
            return StrategyPlan(
                engine="kangaroo",
                threads=threads,
                dp=dp,
                tier=profile.tier,
                reason=(
                    f"tier={profile.tier}: Kangaroo available for "
                    f"{puzzle.bits}-bit pubkey search"
                ),
            )

    if puzzle.bits <= 16:
        return StrategyPlan(
            engine="sequential",
            workers=workers,
            tier=profile.tier,
            reason=f"tier={profile.tier}: tiny {puzzle.bits}-bit range; full sequential",
        )

    if puzzle.bits <= SEQUENTIAL_BITS and range_size <= MAX_SEQUENTIAL_KEYS:
        if range_size > chunk:
            return StrategyPlan(
                engine="sequential",
                workers=workers,
                coverage=True,
                chunk_size=chunk,
                max_chunks=max_chunks,
                tier=profile.tier,
                reason=(
                    f"tier={profile.tier}: {puzzle.bits}-bit range fits sequential; "
                    "coverage in chunks"
                ),
            )
        return StrategyPlan(
            engine="sequential",
            workers=workers,
            tier=profile.tier,
            reason=f"tier={profile.tier}: {puzzle.bits}-bit range; single-pass sequential",
        )

    # Prefer GPU address search when host/tier indicates GPU capability.
    if "bitcrack" in installed and puzzle.bits > SEQUENTIAL_BITS:
        return StrategyPlan(
            engine="bitcrack",
            tier=profile.tier,
            reason=(
                f"tier={profile.tier}: BitCrack available for "
                f"{puzzle.bits}-bit address brute-force"
            ),
        )

    if "keyhunt" in installed:
        return StrategyPlan(
            engine="keyhunt",
            threads=threads,
            tier=profile.tier,
            reason=(
                f"tier={profile.tier}: keyhunt present for {puzzle.bits}-bit address search"
            ),
        )

    if puzzle.practice_solution is not None:
        hints: list[str] = []
        if _has_pubkey(puzzle) and not installed.intersection({"rckangaroo", "kangaroo"}):
            hints.append("install RCKangaroo/Kangaroo for pubkey path")
        if "bitcrack" not in installed and puzzle.bits > SEQUENTIAL_BITS:
            hints.append("install BitCrack for GPU address search")
        hint = f" ({'; '.join(hints)})" if hints else ""
        return StrategyPlan(
            engine="window",
            workers=workers,
            coverage=True,
            chunk_size=chunk,
            window=min(window, max(chunk * 4, 50_000)),
            max_chunks=max_chunks if max_chunks is not None else 2,
            tier=profile.tier,
            reason=(
                f"tier={profile.tier}: {puzzle.bits}-bit full range too large locally; "
                f"practice window + coverage{hint}"
            ),
        )

    # Algorithm-first fallback for unsolved / no-binary hosts.
    if _has_pubkey(puzzle) and puzzle.bits >= PUBKEY_MIN_BITS:
        return StrategyPlan(
            engine="rckangaroo",
            threads=threads,
            dp=dp,
            tier=profile.tier,
            reason=(
                f"tier={profile.tier}: {puzzle.bits}-bit pubkey puzzle; "
                "prefer RCKangaroo/Kangaroo (set RCKANGAROO_PATH or KANGAROO_PATH)"
            ),
        )
    # Algorithm-first when no solver is installed: GPU tiers name BitCrack,
    # otherwise prefer CPU keyhunt so the board matches typical host capability.
    preferred = (
        "bitcrack" if profile.gpu or profile.tier in {"gpu", "compute"} else "keyhunt"
    )
    path_hint = (
        "BITCRACK_PATH or KEYHUNT_PATH"
        if preferred == "bitcrack"
        else "KEYHUNT_PATH or BITCRACK_PATH"
    )
    return StrategyPlan(
        engine=preferred,
        tier=profile.tier,
        reason=(
            f"tier={profile.tier}: {puzzle.bits}-bit address puzzle; "
            f"prefer {preferred} (set {path_hint})"
        ),
    )


def format_host_profile(profile: HostProfile | None = None) -> str:
    host = profile or probe_host()
    lines = [
        f"tier           : {host.tier}",
        f"cpus           : {host.cpus}",
        f"mem_mb         : {host.mem_mb}",
        f"gpu            : {host.gpu}"
        + (f" ({host.gpu_name})" if host.gpu_name else ""),
        f"disk_free_mb   : {host.disk_free_mb if host.disk_free_mb is not None else 'n/a'}",
        f"engines        : {', '.join(sorted(host.engines)) or '(none detected)'}",
        f"overrides      : {', '.join(host.overrides) or '(none)'}",
        "",
        "tier meaning:",
        "  constrained : low RAM/CPU — small chunks, few workers",
        "  standard    : typical 2–8 GiB host — balanced local + external",
        "  gpu         : NVIDIA or GPU solvers present — prefer BitCrack/RCKangaroo",
        "  compute     : high CPU/RAM — larger chunks/windows/threads",
        "",
        "env overrides: BTC_PUZZLE_LAB_CPUS, BTC_PUZZLE_LAB_MEM_MB, BTC_PUZZLE_LAB_GPU, "
        "BTC_PUZZLE_LAB_THREADS, "
        "BTC_PUZZLE_LAB_DP",
    ]
    knobs = _resource_knobs(host)
    lines.extend(
        [
            "",
            "adaptive knobs:",
            f"  workers={knobs['workers']} threads={knobs['threads']} "
            f"chunk={knobs['chunk']} window={knobs['window']} "
            f"max_chunks={knobs['max_chunks']} kangaroo_dp={SAFE_DP}",
        ]
    )
    return "\n".join(lines)


def adapt_recommendations(profile: HostProfile | None = None) -> list[str]:
    """Human-readable next actions for this environment."""
    host = profile or probe_host()
    tips: list[str] = []
    if host.tier == "constrained":
        tips.append("keep batch --limit small; prefer solved/practice ids for local engines")
    if not host.engines:
        tips.append(
            "no external solvers detected — run: btc-puzzle-lab engines install "
            "(keyhunt + kangaroo), or set BITCRACK_PATH / RCKANGAROO_PATH for GPU"
        )
    if host.gpu and "bitcrack" not in host.engines:
        tips.append(
            "GPU seen but BitCrack missing — run: "
            "btc-puzzle-lab engines install --only bitcrack"
        )
    if "rckangaroo" not in host.engines and "kangaroo" not in host.engines:
        tips.append("no kangaroo-class solver — pubkey puzzles will stay blocked until configured")
    if host.disk_free_mb is not None and host.disk_free_mb < 512:
        tips.append("low free disk (<512 MiB) — coverage/batch state may fail to persist")
    # "compute" is the high-CPU/no-GPU tier, so it must not be sent down the GPU path.
    if host.gpu or host.tier == "gpu":
        tips.append(
            "GPU VPS: one card → one puzzle; "
            "btc-puzzle-lab auto 140   (or: once --ids 140 --resource gpu)"
        )
    else:
        tips.append("run: btc-puzzle-lab auto 71   (or: once --resource cpu)")
    return tips


