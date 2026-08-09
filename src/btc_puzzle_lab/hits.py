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


@dataclass(frozen=True)
class AppendHitResult:
    path: Path
    appended: bool
    duplicate: bool


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    return STATE_DIR


def hit_identity(hit: Hit) -> tuple[int, str]:
    return hit.puzzle_id, hit.private_key_hex.lower()


def append_hit(hit: Hit, path: Path | None = None, *, dedupe: bool = True) -> AppendHitResult:
    ensure_state_dir()
    target = path or HITS_FILE
    if dedupe and target.exists():
        for existing in read_hits(target):
            if hit_identity(existing) == hit_identity(hit):
                return AppendHitResult(path=target, appended=False, duplicate=True)
    if target.exists():
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(hit), sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return AppendHitResult(path=target, appended=True, duplicate=False)


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


def unique_hits(hits: list[Hit] | None = None) -> list[Hit]:
    seen: set[tuple[int, str]] = set()
    out: list[Hit] = []
    for hit in hits if hits is not None else read_hits():
        key = hit_identity(hit)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
