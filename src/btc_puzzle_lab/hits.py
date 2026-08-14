from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from btc_puzzle_lab.paths import HITS_FILE, STATE_DIR

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


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
    return STATE_DIR


def hit_identity(hit: Hit) -> tuple[int, str]:
    return hit.puzzle_id, hit.private_key_hex.lower()


def _thread_lock_for(path: Path) -> threading.Lock:
    # fcntl.flock is per-process; ThreadingHTTPServer handlers need a thread lock too.
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def append_hit(hit: Hit, path: Path | None = None, *, dedupe: bool = True) -> AppendHitResult:
    ensure_state_dir()
    target = path or HITS_FILE
    lock_path = target.with_name(target.name + ".lock")
    created_lock = not lock_path.exists()
    with _thread_lock_for(target):
        with open(lock_path, "a+", encoding="utf-8") as lock_fh:
            if created_lock:
                os.chmod(lock_path, 0o600)
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                if dedupe and target.exists():
                    for existing in read_hits(target):
                        if hit_identity(existing) == hit_identity(hit):
                            return AppendHitResult(
                                path=target, appended=False, duplicate=True
                            )
                created = not target.exists()
                with open(target, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(hit), sort_keys=True) + "\n")
                if created:
                    os.chmod(target, 0o600)
                return AppendHitResult(path=target, appended=True, duplicate=False)
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


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
