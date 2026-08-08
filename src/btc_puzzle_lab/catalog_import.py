"""Import the full Bitcoin Puzzle Transaction catalog.

Default source: privatekeys.pw CSV export (public puzzle metadata).
Writes workspace ``data/puzzles.json``, which overrides the packaged practice catalog.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import requests

from btc_puzzle_lab import __version__
from btc_puzzle_lab.crypto import normalize_privkey_hex
from btc_puzzle_lab.paths import workspace_root

DEFAULT_EXPORT_URL = (
    "https://privatekeys.pw/puzzles/bitcoin-puzzle-tx/export?status=all"
)
SOURCE_PAGE = "https://privatekeys.pw/puzzles/bitcoin-puzzle-tx"
_FETCH_HEADERS = {
    "User-Agent": (
        f"Mozilla/5.0 (compatible; btc-puzzle-lab/{__version__}; "
        "+https://github.com/catamitez0-maker/btc-puzzle-lab)"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Referer": SOURCE_PAGE,
}


@dataclass(frozen=True)
class ImportResult:
    path: Path
    count: int
    solved: int
    unsolved: int
    with_pubkey: int
    with_solution: int
    source: str


def default_catalog_path() -> Path:
    return workspace_root() / "data" / "puzzles.json"


def read_bundled_export_csv() -> tuple[str, str]:
    """Return (csv_text, source_label) from workspace or package data."""
    workspace_csv = workspace_root() / "data" / "puzzle-tx-export.csv"
    if workspace_csv.is_file():
        return workspace_csv.read_text(encoding="utf-8"), str(workspace_csv)
    packaged = files("btc_puzzle_lab").joinpath("data/puzzle-tx-export.csv")
    return packaged.read_text(encoding="utf-8"), "package:data/puzzle-tx-export.csv"


def fetch_csv(url: str = DEFAULT_EXPORT_URL, *, timeout: float = 60.0) -> str:
    try:
        resp = requests.get(url, headers=_FETCH_HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"catalog download failed: {exc}") from exc
    if resp.status_code != 200:
        raise RuntimeError(f"catalog download failed HTTP {resp.status_code}: {url}")
    text = resp.text
    if "bits" not in text.splitlines()[0]:
        raise RuntimeError("catalog download did not look like the expected CSV export")
    return text


def _engine_default(bits: int) -> str:
    return "sequential" if bits <= 20 else "window"


def _clean_hex(value: str | None) -> str:
    if value is None:
        return ""
    text = value.strip().lower().removeprefix("0x")
    return text


def rows_from_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    required = {"bits", "range_min", "range_max", "address"}
    missing = required - {name.strip() for name in reader.fieldnames}
    if missing:
        raise ValueError(f"CSV missing columns: {', '.join(sorted(missing))}")

    puzzles: list[dict] = []
    for row in reader:
        bits = int(str(row["bits"]).strip())
        address = str(row["address"]).strip()
        if not address:
            raise ValueError(f"puzzle bits={bits} has empty address")
        range_min = _clean_hex(row.get("range_min"))
        range_max = _clean_hex(row.get("range_max"))
        pubkey = _clean_hex(row.get("public_key"))
        priv = _clean_hex(row.get("private_key"))
        solve_date = (row.get("solve_date") or "").strip()
        btc_value = (row.get("btc_value") or "").strip()
        solved = bool(priv)
        status = "solved" if solved else "unsolved"
        notes_parts = []
        if btc_value:
            notes_parts.append(f"listed_value_btc={btc_value}")
        if solve_date:
            notes_parts.append(f"solve_date={solve_date}")
        if not solved:
            notes_parts.append("no practice solution in export")
        solution_hex = format(int(priv, 16), "x") if priv else None
        if solution_hex is not None:
            # Normalize via crypto helper (rejects empty / invalid lengths later on use).
            normalize_privkey_hex(solution_hex)
        entry = {
            "id": bits,
            "bits": bits,
            "address": address,
            "range_start_hex": range_min,
            "range_end_hex": range_max,
            "pubkey_compressed_hex": pubkey,
            "practice_solution_hex": solution_hex,
            "status": status,
            "engine_default": _engine_default(bits),
            "notes": "; ".join(notes_parts),
        }
        puzzles.append(entry)

    puzzles.sort(key=lambda item: item["id"])
    return puzzles


def build_catalog_document(
    puzzles: list[dict],
    *,
    source: str = SOURCE_PAGE,
    description: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "description": description
        or (
            "Full Bitcoin Puzzle Transaction catalog imported for algorithm work. "
            "practice_solution_hex is present only for publicly solved entries."
        ),
        "source": source,
        "puzzles": puzzles,
    }


def write_catalog(doc: dict, path: Path | None = None) -> Path:
    target = path or default_catalog_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return target


def import_catalog_from_csv_text(
    text: str,
    *,
    output: Path | None = None,
    source: str = SOURCE_PAGE,
    include_solutions: bool = True,
) -> ImportResult:
    puzzles = rows_from_csv(text)
    if not include_solutions:
        for row in puzzles:
            row["practice_solution_hex"] = None
            if row["status"] == "solved":
                row["notes"] = (row["notes"] + "; solutions omitted by import flag").strip(
                    "; "
                )
    doc = build_catalog_document(puzzles, source=source)
    path = write_catalog(doc, output)
    solved = sum(1 for p in puzzles if p["status"] == "solved")
    unsolved = len(puzzles) - solved
    with_pubkey = sum(1 for p in puzzles if p.get("pubkey_compressed_hex"))
    with_solution = sum(1 for p in puzzles if p.get("practice_solution_hex"))
    return ImportResult(
        path=path,
        count=len(puzzles),
        solved=solved,
        unsolved=unsolved,
        with_pubkey=with_pubkey,
        with_solution=with_solution,
        source=source,
    )


def import_catalog(
    *,
    url: str | None = None,
    csv_path: Path | None = None,
    output: Path | None = None,
    include_solutions: bool = True,
) -> ImportResult:
    if csv_path is not None and url is not None:
        raise ValueError("pass only one of url or csv_path")
    if csv_path is not None:
        text = csv_path.read_text(encoding="utf-8")
        source = str(csv_path)
    elif url is not None:
        text = fetch_csv(url)
        source = url
    else:
        text, source = read_bundled_export_csv()
    return import_catalog_from_csv_text(
        text,
        output=output,
        source=source,
        include_solutions=include_solutions,
    )
