from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
PUZZLES_FILE = DATA_DIR / "puzzles.json"
STATE_DIR = REPO_ROOT / "state"
HITS_FILE = STATE_DIR / "HITS.jsonl"
RUNS_FILE = STATE_DIR / "runs.jsonl"
CONFIG_DIR = REPO_ROOT / "config"
ENV_FILE = CONFIG_DIR / ".env"
ENV_EXAMPLE_FILE = CONFIG_DIR / ".env.example"


def scan_checkpoint_path(puzzle_id: int) -> Path:
    return STATE_DIR / f"scan_{puzzle_id}.json"
