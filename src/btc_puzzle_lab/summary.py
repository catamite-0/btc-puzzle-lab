"""Local pipeline summary (hits / dry-runs / recent events)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab.catalog import load_puzzles
from btc_puzzle_lab.hits import read_hits, unique_hits
from btc_puzzle_lab.paths import HITS_FILE, RUNS_FILE, STATE_DIR
from btc_puzzle_lab.runlog import read_events


@dataclass(frozen=True)
class LabSummary:
    catalog_count: int
    hit_rows: int
    unique_hits: int
    puzzles_hit: list[int]
    dry_run_files: int
    recent_events: list[dict]
    hits_path: str
    runs_path: str


def list_dry_run_files(state_dir: Path | None = None) -> list[Path]:
    root = state_dir or STATE_DIR
    if not root.exists():
        return []
    return sorted(root.glob("dryrun_*.txhex"))


def build_summary(*, recent: int = 10, state_dir: Path | None = None) -> LabSummary:
    root = state_dir or STATE_DIR
    hits = read_hits(HITS_FILE if state_dir is None else root / "HITS.jsonl")
    uniq = unique_hits(hits)
    puzzles = sorted({h.puzzle_id for h in uniq})
    runs_path = RUNS_FILE if state_dir is None else root / "runs.jsonl"
    return LabSummary(
        catalog_count=len(load_puzzles()),
        hit_rows=len(hits),
        unique_hits=len(uniq),
        puzzles_hit=puzzles,
        dry_run_files=len(list_dry_run_files(root)),
        recent_events=read_events(runs_path, limit=recent),
        hits_path=str(HITS_FILE if state_dir is None else root / "HITS.jsonl"),
        runs_path=str(runs_path),
    )


def format_summary(summary: LabSummary) -> str:
    lines = [
        f"catalog puzzles : {summary.catalog_count}",
        f"hit rows        : {summary.hit_rows}",
        f"unique hits     : {summary.unique_hits}",
        f"puzzles hit     : {', '.join(map(str, summary.puzzles_hit)) or '(none)'}",
        f"dry-run files   : {summary.dry_run_files}",
        f"hits file       : {summary.hits_path}",
        f"runs file       : {summary.runs_path}",
    ]
    if summary.recent_events:
        lines.append("recent events:")
        for row in summary.recent_events:
            event = row.get("event", "?")
            ts = row.get("ts", "?")
            extra = {
                k: v
                for k, v in row.items()
                if k not in {"event", "ts"} and not str(k).startswith("_")
            }
            detail = " ".join(f"{k}={v}" for k, v in sorted(extra.items())[:6])
            lines.append(f"  {ts} {event}" + (f" {detail}" if detail else ""))
    else:
        lines.append("recent events  : (none)")
    return "\n".join(lines)
