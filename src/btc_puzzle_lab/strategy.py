"""Host-aware search strategy for `run --auto`.

Flat decision table: probe host once, then return one plan.
No kangaroo wiring yet — we only choose among local engines.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.search import DEFAULT_CHUNK_SIZE, MAX_SEQUENTIAL_KEYS

SEQUENTIAL_BITS = 20
LOW_MEM_MB = 2048


@dataclass(frozen=True)
class HostProfile:
    cpus: int
    mem_mb: int
    has_keyhunt: bool


@dataclass(frozen=True)
class StrategyPlan:
    engine: str
    reason: str
    workers: int = 1
    threads: int = 2
    coverage: bool = False
    chunk_size: int = DEFAULT_CHUNK_SIZE
    order: str = "sequential"
    seed: int | None = None
    window: int = 1_000_000
    max_chunks: int | None = None

    def format(self) -> str:
        bits = [
            f"engine={self.engine}",
            f"workers={self.workers}",
            f"coverage={self.coverage}",
        ]
        if self.coverage:
            bits.append(f"chunk_size={self.chunk_size}")
            bits.append(f"order={self.order}")
            if self.max_chunks is not None:
                bits.append(f"max_chunks={self.max_chunks}")
        if self.engine == "window":
            bits.append(f"window={self.window}")
        if self.engine == "keyhunt":
            bits.append(f"threads={self.threads}")
        return f"{' '.join(bits)} — {self.reason}"


def _mem_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return LOW_MEM_MB


def _has_keyhunt() -> bool:
    env = os.environ.get("KEYHUNT_PATH")
    candidates = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        (
            Path("/home/dev/projects/coinsense/bin/keyhunt"),
            Path.cwd() / "bin" / "keyhunt",
        )
    )
    return any(path.is_file() and os.access(path, os.X_OK) for path in candidates)


def probe_host() -> HostProfile:
    return HostProfile(
        cpus=max(1, os.cpu_count() or 1),
        mem_mb=_mem_mb(),
        has_keyhunt=_has_keyhunt(),
    )


def plan_strategy(puzzle: Puzzle, host: HostProfile | None = None) -> StrategyPlan:
    """Choose one engine/config for this puzzle on this host."""
    profile = host or probe_host()
    workers = 1 if profile.mem_mb < LOW_MEM_MB else min(2, profile.cpus)
    threads = min(max(1, profile.cpus), 4)
    chunk = 16_384 if profile.mem_mb < LOW_MEM_MB else DEFAULT_CHUNK_SIZE
    range_size = puzzle.range_end - puzzle.range_start + 1
    pubkey_hint = (
        " (every-5th/pubkey-class: kangaroo would be better if installed)"
        if puzzle.id % 5 == 0
        else ""
    )

    if puzzle.bits <= 16:
        return StrategyPlan(
            engine="sequential",
            workers=workers,
            reason=f"tiny {puzzle.bits}-bit range; full sequential",
        )

    if puzzle.bits <= SEQUENTIAL_BITS and range_size <= MAX_SEQUENTIAL_KEYS:
        if range_size > chunk:
            return StrategyPlan(
                engine="sequential",
                workers=workers,
                coverage=True,
                chunk_size=chunk,
                max_chunks=4,
                reason=f"{puzzle.bits}-bit range fits sequential; coverage in chunks",
            )
        return StrategyPlan(
            engine="sequential",
            workers=workers,
            reason=f"{puzzle.bits}-bit range; single-pass sequential",
        )

    if profile.has_keyhunt:
        return StrategyPlan(
            engine="keyhunt",
            threads=threads,
            reason=f"keyhunt present for {puzzle.bits}-bit search{pubkey_hint}",
        )

    if puzzle.practice_solution is not None:
        return StrategyPlan(
            engine="window",
            workers=workers,
            coverage=True,
            chunk_size=chunk,
            window=min(1_000_000, max(chunk * 4, 50_000)),
            max_chunks=2,
            reason=(
                f"{puzzle.bits}-bit full range too large here; "
                f"practice window + coverage{pubkey_hint}"
            ),
        )

    return StrategyPlan(
        engine="inject-known",
        reason="no safe local search path; catalog inject only",
    )
