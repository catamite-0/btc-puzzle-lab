from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab.paths import PUZZLES_FILE, read_puzzles_json


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


def load_puzzles(path: Path | None = None) -> list[Puzzle]:
    if path is not None:
        raw = path.read_text(encoding="utf-8")
    elif Path(PUZZLES_FILE).is_file():
        raw = Path(PUZZLES_FILE).read_text(encoding="utf-8")
    else:
        raw = read_puzzles_json()
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
                pubkey_compressed_hex=row["pubkey_compressed_hex"],
                practice_solution=int(sol, 16) if sol else None,
                status=row.get("status", "unknown"),
                engine_default=row.get("engine_default", "sequential"),
                notes=row.get("notes", ""),
            )
        )
    return puzzles


def get_puzzle(puzzle_id: int, path: Path | None = None) -> Puzzle:
    for puzzle in load_puzzles(path):
        if puzzle.id == puzzle_id:
            return puzzle
    raise KeyError(f"unknown puzzle #{puzzle_id} (not in practice catalog)")
