"""Range coverage ledger for chunked / random puzzle scans.

Tracks which sub-ranges of a puzzle keyspace are pending, in-progress, done,
or hit — without storing private keys.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from btc_puzzle_lab.hits import ensure_state_dir, utc_now
from btc_puzzle_lab.paths import coverage_path

CHUNK_STATUSES = ("pending", "in_progress", "done", "hit")
ORDERS = ("sequential", "random")


@dataclass
class Chunk:
    index: int
    start: int
    end: int
    status: str = "pending"
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.status not in CHUNK_STATUSES:
            raise ValueError(f"invalid chunk status: {self.status}")
        if not self.updated_at:
            self.updated_at = utc_now()

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "start_hex": f"{self.start:x}",
            "end_hex": f"{self.end:x}",
            "status": self.status,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, row: dict) -> Chunk:
        return cls(
            index=int(row["index"]),
            start=int(row["start"]),
            end=int(row["end"]),
            status=str(row.get("status", "pending")),
            updated_at=str(row.get("updated_at") or utc_now()),
        )


@dataclass
class CoverageLedger:
    puzzle_id: int
    range_start: int
    range_end: int
    chunk_size: int
    chunks: list[Chunk] = field(default_factory=list)
    updated_at: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if self.range_start > self.range_end:
            raise ValueError("invalid coverage range")
        if not self.updated_at:
            self.updated_at = utc_now()
        if not self.chunks:
            self.chunks = build_chunks(self.range_start, self.range_end, self.chunk_size)

    @property
    def total_keys(self) -> int:
        return self.range_end - self.range_start + 1

    @property
    def covered_keys(self) -> int:
        return sum(c.size for c in self.chunks if c.status in {"done", "hit"})

    @property
    def coverage_ratio(self) -> float:
        total = self.total_keys
        return (self.covered_keys / total) if total else 0.0

    def counts(self) -> dict[str, int]:
        out = {name: 0 for name in CHUNK_STATUSES}
        for chunk in self.chunks:
            out[chunk.status] = out.get(chunk.status, 0) + 1
        return out

    def compatible_with(self, *, range_start: int, range_end: int, chunk_size: int) -> bool:
        return (
            self.range_start == range_start
            and self.range_end == range_end
            and self.chunk_size == chunk_size
        )

    def mark(self, index: int, status: str) -> Chunk:
        if status not in CHUNK_STATUSES:
            raise ValueError(f"invalid chunk status: {status}")
        chunk = self.chunks[index]
        chunk.status = status
        chunk.updated_at = utc_now()
        self.updated_at = chunk.updated_at
        return chunk

    def plan(
        self,
        *,
        order: str = "sequential",
        seed: int | None = None,
        max_chunks: int | None = None,
    ) -> list[Chunk]:
        if order not in ORDERS:
            raise ValueError(f"order must be one of {ORDERS}")
        in_progress = [c for c in self.chunks if c.status == "in_progress"]
        pending = [c for c in self.chunks if c.status == "pending"]
        if order == "random":
            rng = random.Random(seed)
            rng.shuffle(pending)
        selected = in_progress + pending
        if max_chunks is not None:
            if max_chunks < 1:
                raise ValueError("max_chunks must be >= 1")
            selected = selected[:max_chunks]
        return selected

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "puzzle_id": self.puzzle_id,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "range_start_hex": f"{self.range_start:x}",
            "range_end_hex": f"{self.range_end:x}",
            "chunk_size": self.chunk_size,
            "updated_at": self.updated_at,
            "stats": {
                "total_keys": self.total_keys,
                "covered_keys": self.covered_keys,
                "coverage_ratio": round(self.coverage_ratio, 6),
                "chunk_counts": self.counts(),
            },
            "chunks": [c.to_dict() for c in self.chunks],
        }

    @classmethod
    def from_dict(cls, row: dict) -> CoverageLedger:
        chunks = [Chunk.from_dict(c) for c in row.get("chunks", [])]
        return cls(
            puzzle_id=int(row["puzzle_id"]),
            range_start=int(row["range_start"]),
            range_end=int(row["range_end"]),
            chunk_size=int(row["chunk_size"]),
            chunks=chunks,
            updated_at=str(row.get("updated_at") or utc_now()),
            schema_version=int(row.get("schema_version", 1)),
        )


def build_chunks(range_start: int, range_end: int, chunk_size: int) -> list[Chunk]:
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if range_start > range_end:
        return []
    chunks: list[Chunk] = []
    cursor = range_start
    index = 0
    now = utc_now()
    while cursor <= range_end:
        end = min(range_end, cursor + chunk_size - 1)
        chunks.append(
            Chunk(index=index, start=cursor, end=end, status="pending", updated_at=now)
        )
        cursor = end + 1
        index += 1
    return chunks


def load_coverage(puzzle_id: int, path: Path | None = None) -> CoverageLedger | None:
    target = path or coverage_path(puzzle_id)
    if not target.exists():
        return None
    return CoverageLedger.from_dict(json.loads(target.read_text(encoding="utf-8")))


def save_coverage(ledger: CoverageLedger, path: Path | None = None) -> Path:
    ensure_state_dir()
    target = path or coverage_path(ledger.puzzle_id)
    ledger.updated_at = utc_now()
    target.write_text(
        json.dumps(ledger.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    return target


def get_or_create_coverage(
    puzzle_id: int,
    *,
    range_start: int,
    range_end: int,
    chunk_size: int,
    path: Path | None = None,
    reset_if_mismatch: bool = True,
) -> tuple[CoverageLedger, bool]:
    """Return (ledger, created_or_reset)."""
    existing = load_coverage(puzzle_id, path=path)
    if existing is not None and existing.compatible_with(
        range_start=range_start, range_end=range_end, chunk_size=chunk_size
    ):
        return existing, False
    if existing is not None and not reset_if_mismatch:
        raise ValueError(
            f"coverage for puzzle #{puzzle_id} exists with different range/chunk_size"
        )
    ledger = CoverageLedger(
        puzzle_id=puzzle_id,
        range_start=range_start,
        range_end=range_end,
        chunk_size=chunk_size,
    )
    save_coverage(ledger, path=path)
    return ledger, True


def format_coverage(ledger: CoverageLedger) -> str:
    counts = ledger.counts()
    lines = [
        f"puzzle #{ledger.puzzle_id}",
        f"  range     : {ledger.range_start:x}:{ledger.range_end:x}",
        f"  chunk_size: {ledger.chunk_size:,}",
        f"  chunks    : {len(ledger.chunks)} "
        f"(pending={counts['pending']} in_progress={counts['in_progress']} "
        f"done={counts['done']} hit={counts['hit']})",
        f"  covered   : {ledger.covered_keys:,}/{ledger.total_keys:,} "
        f"({ledger.coverage_ratio:.2%})",
        f"  updated   : {ledger.updated_at}",
    ]
    return "\n".join(lines)
