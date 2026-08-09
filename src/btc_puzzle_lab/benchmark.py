"""Bounded, non-chain GPU benchmark for the pinned BitCrack adapter."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import base58

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.crypto import is_valid_btc_address
from btc_puzzle_lab.engines import ExternalEngineResult, run_external_engine
from btc_puzzle_lab.paths import HITS_FILE, workspace_root

SYNTHETIC_PUZZLE_ID_BASE = 900_000_000
SYNTHETIC_PUZZLE_ID_SPACE = 100_000_000
SYNTHETIC_BITS = 72
SYNTHETIC_RANGE_START = 1 << (SYNTHETIC_BITS - 1)
SYNTHETIC_RANGE_END = (1 << SYNTHETIC_BITS) - 1
MIN_BENCHMARK_SECONDS = 75.0
MAX_BENCHMARK_SECONDS = 90.0
DEFAULT_BENCHMARK_SECONDS = 90.0

_NON_NETWORK_PREFIXES = "ZYXWVUTSRQPNMLKJHGFEDCBA98765432"


@dataclass(frozen=True)
class BitCrackCheckpoint:
    start: int
    next_key: int
    end: int
    elapsed_ms: int
    stride: int
    blocks: int
    threads: int
    points: int
    compression: str
    device: int

    @property
    def grid(self) -> tuple[int, int, int, str, int]:
        return (self.blocks, self.threads, self.points, self.compression, self.device)


@dataclass(frozen=True)
class BenchmarkRound:
    advanced_keys: int
    elapsed_ms: int
    mkeys_per_second: float


@dataclass(frozen=True)
class SyntheticBenchmarkResult:
    target_fingerprint: str
    checkpoint_path: Path
    rounds: tuple[BenchmarkRound, BenchmarkRound]
    grid: tuple[int, int, int, str, int]


EngineRunner = Callable[..., ExternalEngineResult]


def synthetic_bitcrack_target(entropy: bytes | None = None) -> str:
    """Return an ephemeral random BitCrack target string.

    The pinned BitCrack commit validates the Base58Check body after dropping one
    prefix character. A non-network prefix gives it a random 160-bit workload
    without accepting a chain address as input. No private scalar is generated.
    """
    digest = entropy if entropy is not None else secrets.token_bytes(20)
    if len(digest) != 20:
        raise ValueError("synthetic target entropy must be exactly 20 bytes")
    body = base58.b58encode_check(digest).decode("ascii")
    for prefix in _NON_NETWORK_PREFIXES:
        target = prefix + body
        if len(target) > 34 or is_valid_btc_address(target):
            continue
        try:
            base58.b58decode_check(target)
        except ValueError:
            return target
    raise RuntimeError("could not construct a non-network synthetic input string")


def synthetic_puzzle(*, puzzle_id: int, entropy: bytes | None = None) -> Puzzle:
    if not SYNTHETIC_PUZZLE_ID_BASE <= puzzle_id < (
        SYNTHETIC_PUZZLE_ID_BASE + SYNTHETIC_PUZZLE_ID_SPACE
    ):
        raise ValueError("synthetic puzzle id is outside the reserved range")
    return Puzzle(
        id=puzzle_id,
        bits=SYNTHETIC_BITS,
        address=synthetic_bitcrack_target(entropy),
        range_start=SYNTHETIC_RANGE_START,
        range_end=SYNTHETIC_RANGE_END,
        pubkey_compressed_hex="",
        practice_solution=None,
        status="synthetic",
        engine_default="bitcrack",
        notes="ephemeral random throughput target; no known key or funds; never fund",
    )


def parse_bitcrack_checkpoint(text: str) -> BitCrackCheckpoint:
    fields: dict[str, str] = {}
    if len(text.encode("utf-8")) > 4096:
        raise ValueError("BitCrack checkpoint is unexpectedly large")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not value or key in fields:
            raise ValueError("malformed BitCrack checkpoint")
        fields[key] = value

    required = {
        "start",
        "next",
        "end",
        "elapsed",
        "stride",
        "blocks",
        "threads",
        "points",
        "compression",
        "device",
    }
    missing = sorted(required - fields.keys())
    if missing:
        raise ValueError(f"BitCrack checkpoint is missing: {', '.join(missing)}")
    try:
        checkpoint = BitCrackCheckpoint(
            start=int(fields["start"], 16),
            next_key=int(fields["next"], 16),
            end=int(fields["end"], 16),
            elapsed_ms=int(fields["elapsed"], 10),
            stride=int(fields["stride"], 16),
            blocks=int(fields["blocks"], 10),
            threads=int(fields["threads"], 10),
            points=int(fields["points"], 10),
            compression=fields["compression"].lower(),
            device=int(fields["device"], 10),
        )
    except ValueError as exc:
        raise ValueError("BitCrack checkpoint contains an invalid number") from exc
    if checkpoint.stride != 1:
        raise ValueError("synthetic benchmark requires BitCrack stride=1")
    if checkpoint.compression != "compressed":
        raise ValueError("synthetic benchmark requires compressed points")
    if min(checkpoint.blocks, checkpoint.threads, checkpoint.points) <= 0:
        raise ValueError("BitCrack checkpoint has an invalid GPU grid")
    if checkpoint.device < 0:
        raise ValueError("BitCrack checkpoint has an invalid device index")
    if not checkpoint.start < checkpoint.next_key <= checkpoint.end:
        raise ValueError("BitCrack checkpoint cursor is outside its range")
    if checkpoint.elapsed_ms <= 0:
        raise ValueError("BitCrack checkpoint elapsed time must be positive")
    return checkpoint


def read_bitcrack_checkpoint(path: Path) -> BitCrackCheckpoint:
    return parse_bitcrack_checkpoint(path.read_text(encoding="utf-8"))


def _file_fingerprint(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_fixed_range(checkpoint: BitCrackCheckpoint) -> None:
    if (
        checkpoint.start != SYNTHETIC_RANGE_START
        or checkpoint.end != SYNTHETIC_RANGE_END
    ):
        raise ValueError("checkpoint does not belong to the synthetic benchmark range")


def _round_result(
    before: BitCrackCheckpoint | None,
    after: BitCrackCheckpoint,
) -> BenchmarkRound:
    before_key = before.next_key if before is not None else SYNTHETIC_RANGE_START
    before_elapsed = before.elapsed_ms if before is not None else 0
    advanced = after.next_key - before_key
    elapsed = after.elapsed_ms - before_elapsed
    if advanced <= 0:
        raise RuntimeError("BitCrack checkpoint cursor did not advance")
    if elapsed < 50_000:
        raise RuntimeError("BitCrack checkpoint elapsed time did not advance by 50 seconds")
    return BenchmarkRound(
        advanced_keys=advanced,
        elapsed_ms=elapsed,
        mkeys_per_second=advanced / (elapsed * 1000.0),
    )


def run_synthetic_gpu_benchmark(
    *,
    seconds: float = DEFAULT_BENCHMARK_SECONDS,
    progress: bool = True,
    runner: EngineRunner | None = None,
) -> SyntheticBenchmarkResult:
    """Run two bounded sessions and prove that BitCrack resumes its checkpoint."""
    if not MIN_BENCHMARK_SECONDS <= seconds <= MAX_BENCHMARK_SECONDS:
        raise ValueError(
            f"--seconds must be between {MIN_BENCHMARK_SECONDS:g} "
            f"and {MAX_BENCHMARK_SECONDS:g}"
        )

    state_dir = workspace_root() / "state"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass
    puzzle: Puzzle | None = None
    checkpoint_path: Path | None = None
    for _ in range(32):
        puzzle_id = SYNTHETIC_PUZZLE_ID_BASE + secrets.randbelow(SYNTHETIC_PUZZLE_ID_SPACE)
        candidate = state_dir / f"bitcrack_{puzzle_id}.continue"
        if not candidate.exists():
            puzzle = synthetic_puzzle(puzzle_id=puzzle_id)
            checkpoint_path = candidate
            break
    if puzzle is None or checkpoint_path is None:
        raise RuntimeError("could not allocate a fresh synthetic benchmark checkpoint")

    hits_path = Path(HITS_FILE)
    hits_before = _file_fingerprint(hits_path)
    execute = runner or run_external_engine
    round_results: list[BenchmarkRound] = []
    previous: BitCrackCheckpoint | None = None
    for _ in range(2):
        result = execute(
            puzzle,
            "bitcrack",
            timeout=seconds,
            progress=progress,
            display_command=False,
        )
        if result.secret is not None:
            raise RuntimeError("benchmark runner returned key material; result discarded")
        if not checkpoint_path.is_file():
            raise RuntimeError(
                "BitCrack did not write a checkpoint during the bounded session; "
                f"engine reported: {result.message}"
            )
        current = read_bitcrack_checkpoint(checkpoint_path)
        _validate_fixed_range(current)
        if previous is not None and current.grid != previous.grid:
            raise RuntimeError("BitCrack checkpoint GPU configuration changed between rounds")
        round_results.append(_round_result(previous, current))
        previous = current
        if _file_fingerprint(hits_path) != hits_before:
            raise RuntimeError("synthetic benchmark must not create or modify HITS.jsonl")

    fingerprint = hashlib.sha256(puzzle.address.encode("ascii")).hexdigest()[:12]
    return SyntheticBenchmarkResult(
        target_fingerprint=fingerprint,
        checkpoint_path=checkpoint_path,
        rounds=(round_results[0], round_results[1]),
        grid=previous.grid,
    )


def format_synthetic_gpu_benchmark(result: SyntheticBenchmarkResult) -> str:
    lines = [
        "synthetic GPU benchmark: PASS",
        "target: ephemeral random hash / no known key or funds / never fund",
        f"target fingerprint: {result.target_fingerprint}",
        f"checkpoint: {result.checkpoint_path}",
        (
            "grid: "
            f"blocks={result.grid[0]} threads={result.grid[1]} points={result.grid[2]} "
            f"compression={result.grid[3]} device={result.grid[4]}"
        ),
    ]
    for index, item in enumerate(result.rounds, start=1):
        lines.append(
            f"round {index}: {item.mkeys_per_second:.2f} MKey/s, "
            f"checkpoint elapsed +{item.elapsed_ms / 1000.0:.1f}s"
        )
    lines.append("resume validation: checkpoint advanced in both bounded rounds")
    return "\n".join(lines)
