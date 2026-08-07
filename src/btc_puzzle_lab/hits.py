from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from btc_puzzle_lab.paths import HITS_FILE, STATE_DIR


@dataclass(frozen=True)
class Hit:
    puzzle_id: int
    address: str
    private_key_hex: str
    engine: str
    found_at: str
    verified: bool


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STATE_DIR


def append_hit(hit: Hit, path: Path | None = None) -> Path:
    ensure_state_dir()
    target = path or HITS_FILE
    created = not target.exists()
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(hit), sort_keys=True) + "\n")
    if created:
        os.chmod(target, 0o600)
    return target


def read_hits(path: Path | None = None) -> list[Hit]:
    target = path or HITS_FILE
    if not target.exists():
        return []
    hits: list[Hit] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        hits.append(Hit(**row))
    return hits


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
