from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.crypto import (
    normalize_privkey_hex,
    privkey_bytes,
    privkey_to_p2pkh_address,
    sequential_find_p2pkh,
)
from btc_puzzle_lab.hits import Hit, append_hit, utc_now


@dataclass(frozen=True)
class SearchOutcome:
    hit: Hit | None
    engine: str
    message: str


def _make_hit(puzzle: Puzzle, secret: int, engine: str) -> Hit:
    pk_hex = f"{secret:064x}"
    address = privkey_to_p2pkh_address(privkey_bytes(pk_hex))
    if address != puzzle.address:
        raise RuntimeError("derived address does not match puzzle catalog")
    return Hit(
        puzzle_id=puzzle.id,
        address=address,
        private_key_hex=pk_hex,
        engine=engine,
        found_at=utc_now(),
        verified=True,
    )


def run_sequential(puzzle: Puzzle, *, start: int | None = None, end: int | None = None) -> SearchOutcome:
    lo = puzzle.range_start if start is None else start
    hi = puzzle.range_end if end is None else end
    if hi - lo > 2_000_000:
        return SearchOutcome(
            hit=None,
            engine="sequential",
            message=(
                f"range too large for sequential engine ({hi - lo + 1:,} keys). "
                "Use --window for practice, or --engine keyhunt if configured."
            ),
        )
    print(f"sequential scan puzzle #{puzzle.id} range {lo:x}:{hi:x}", flush=True)
    secret = sequential_find_p2pkh(puzzle.address, lo, hi)
    if secret is None:
        return SearchOutcome(hit=None, engine="sequential", message="no match in range")
    hit = _make_hit(puzzle, secret, "sequential")
    append_hit(hit)
    return SearchOutcome(hit=hit, engine="sequential", message="hit recorded")


def run_window(puzzle: Puzzle, *, window: int = 1_000_000) -> SearchOutcome:
    if puzzle.practice_solution is None:
        return SearchOutcome(
            hit=None,
            engine="window",
            message="puzzle has no practice_solution_hex; cannot center a window",
        )
    if window < 1:
        raise ValueError("window must be >= 1")
    center = puzzle.practice_solution
    half = window // 2
    lo = max(puzzle.range_start, center - half)
    hi = min(puzzle.range_end, center + half)
    print(
        f"practice window scan puzzle #{puzzle.id} "
        f"[{lo:x}, {hi:x}] ({hi - lo + 1:,} keys)",
        flush=True,
    )
    secret = sequential_find_p2pkh(puzzle.address, lo, hi)
    if secret is None:
        return SearchOutcome(hit=None, engine="window", message="no match in practice window")
    hit = _make_hit(puzzle, secret, "window")
    append_hit(hit)
    return SearchOutcome(hit=hit, engine="window", message="hit recorded")


def run_inject_known(puzzle: Puzzle) -> SearchOutcome:
    """Practice-only: record the catalog solution after local verification."""
    if puzzle.practice_solution is None:
        return SearchOutcome(
            hit=None,
            engine="inject-known",
            message="no practice_solution_hex in catalog",
        )
    hit = _make_hit(puzzle, puzzle.practice_solution, "inject-known")
    append_hit(hit)
    return SearchOutcome(hit=hit, engine="inject-known", message="known solution recorded")


def resolve_keyhunt_path() -> Path | None:
    env = os.environ.get("KEYHUNT_PATH")
    if env:
        path = Path(env).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    candidates = [
        Path("/home/dev/projects/coinsense/bin/keyhunt"),
        Path.cwd() / "bin" / "keyhunt",
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def run_keyhunt(puzzle: Puzzle, *, threads: int = 2) -> SearchOutcome:
    binary = resolve_keyhunt_path()
    if binary is None:
        return SearchOutcome(
            hit=None,
            engine="keyhunt",
            message="keyhunt not found; set KEYHUNT_PATH or place bin/keyhunt",
        )
    with tempfile.TemporaryDirectory(prefix="btc-puzzle-lab-") as tmp:
        target = Path(tmp) / "target.txt"
        target.write_text(puzzle.address + "\n", encoding="utf-8")
        cmd = [
            str(binary),
            "-m",
            "address",
            "-f",
            str(target),
            "-b",
            str(puzzle.bits),
            "-l",
            "compress",
            "-t",
            str(max(1, threads)),
            "-s",
            "5",
            "-q",
        ]
        print("running:", " ".join(cmd), flush=True)
        proc = subprocess.run(
            cmd,
            cwd=tmp,
            capture_output=True,
            text=True,
            check=False,
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        secret = _parse_keyhunt_privkey(output)
        if secret is None:
            return SearchOutcome(
                hit=None,
                engine="keyhunt",
                message=f"keyhunt exited {proc.returncode}; no private key parsed",
            )
        hit = _make_hit(puzzle, secret, "keyhunt")
        append_hit(hit)
        return SearchOutcome(hit=hit, engine="keyhunt", message="hit recorded")


def _parse_keyhunt_privkey(output: str) -> int | None:
    for line in output.splitlines():
        lower = line.lower()
        if "private key" in lower or "privkey" in lower:
            parts = line.replace(":", " ").split()
            for part in parts:
                token = part.lower().removeprefix("0x")
                if 1 <= len(token) <= 64 and all(c in "0123456789abcdef" for c in token):
                    try:
                        return int(normalize_privkey_hex(token), 16)
                    except ValueError:
                        continue
    # Also accept KEYHUNT HIT files if present in CWD of subprocess — handled by stdout primarily.
    return None


def run_puzzle(
    puzzle: Puzzle,
    *,
    engine: str | None = None,
    window: int = 1_000_000,
    threads: int = 2,
) -> SearchOutcome:
    choice = (engine or puzzle.engine_default).lower()
    if choice in {"sequential", "seq"}:
        return run_sequential(puzzle)
    if choice in {"window", "practice-window"}:
        return run_window(puzzle, window=window)
    if choice in {"inject", "inject-known", "known"}:
        return run_inject_known(puzzle)
    if choice == "keyhunt":
        return run_keyhunt(puzzle, threads=threads)
    return SearchOutcome(hit=None, engine=choice, message=f"unknown engine: {choice}")
