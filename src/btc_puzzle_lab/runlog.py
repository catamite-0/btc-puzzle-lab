"""Structured run log (no private keys / tx hex)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from btc_puzzle_lab.hits import ensure_state_dir, utc_now
from btc_puzzle_lab.paths import RUNS_FILE

_SENSITIVE_KEYS = {
    "private_key",
    "private_key_hex",
    "privkey",
    "tx_hex",
    "signed_tx",
    "wif",
}


def _sanitize_value(value: Any) -> Any:
    # Lists matter as much as dicts: a payload like {"hits": [{"private_key_hex": …}]}
    # used to pass straight through, because only dicts were walked.
    if isinstance(value, dict):
        return {
            key: _sanitize_value(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    return value


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_value(fields)


def log_event(event: str, *, log_path: Path | None = None, **fields: Any) -> Path:
    ensure_state_dir()
    target = log_path or RUNS_FILE
    created = not target.exists()
    row = {"ts": utc_now(), "event": event, **_sanitize(fields)}
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    if created:
        os.chmod(target, 0o600)
    return target


def read_events(log_path: Path | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
    target = log_path or RUNS_FILE
    if not target.exists():
        return []
    rows = [
        json.loads(line)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if limit is not None:
        return rows[-limit:]
    return rows
