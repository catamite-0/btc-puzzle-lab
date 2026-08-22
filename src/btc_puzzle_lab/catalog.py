from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from btc_puzzle_lab.paths import read_puzzles_json


@dataclass(frozen=True)
class Puzzle:
    id: int
    bits: int
    address: str
    range_start: int
    range_end: int
    pubkey_compressed_hex: str
    practice_solution: int | None
    status: str
    engine_default: str
    notes: str

    @property
    def range_start_hex(self) -> str:
        return f"{self.range_start:x}"

    @property
    def range_end_hex(self) -> str:
        return f"{self.range_end:x}"


def _parse_puzzles(raw: str) -> list[Puzzle]:
    data = json.loads(raw)
    puzzles: list[Puzzle] = []
    for row in data["puzzles"]:
        sol = row.get("practice_solution_hex")
        puzzles.append(
            Puzzle(
                id=int(row["id"]),
                bits=int(row["bits"]),
                address=row["address"],
                range_start=int(row["range_start_hex"], 16),
                range_end=int(row["range_end_hex"], 16),
                pubkey_compressed_hex=row.get("pubkey_compressed_hex") or "",
                practice_solution=int(sol, 16) if sol else None,
                status=row.get("status", "unknown"),
                engine_default=row.get("engine_default", "sequential"),
                notes=row.get("notes", ""),
            )
        )
    return puzzles


def load_packaged_full_puzzles() -> list[Puzzle]:
    """Build the complete catalog from the package-owned CSV snapshot only.

    Deliberately do not call ``read_bundled_export_csv`` here: that legacy
    helper prefers an ignored workspace copy.  A planning snapshot must have a
    deterministic package source and must not require ``auto`` to write
    ``data/puzzles.json`` first.
    """

    from btc_puzzle_lab.catalog_import import build_catalog_document, rows_from_csv

    raw = files("btc_puzzle_lab").joinpath("data/puzzle-tx-export.csv").read_text(encoding="utf-8")
    document = build_catalog_document(
        rows_from_csv(raw),
        source="package:data/puzzle-tx-export.csv",
    )
    return _parse_puzzles(json.dumps(document))


def load_puzzles(path: Path | None = None) -> list[Puzzle]:
    if path is not None:
        raw = path.read_text(encoding="utf-8")
    else:
        raw = read_puzzles_json()
    return _parse_puzzles(raw)


def get_puzzle(puzzle_id: int, path: Path | None = None) -> Puzzle:
    for puzzle in load_puzzles(path):
        if puzzle.id == puzzle_id:
            return puzzle
    raise KeyError(f"unknown puzzle #{puzzle_id} (not in active catalog)")
