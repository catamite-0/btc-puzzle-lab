from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.coverage import (
    CoverageLedger,
    format_coverage,
    get_or_create_coverage,
    save_coverage,
)
from btc_puzzle_lab.crypto import (
    privkey_bytes,
    privkey_to_p2pkh_address,
    sequential_find_p2pkh,
    sequential_find_p2pkh_parallel,
)
from btc_puzzle_lab.engines import ENGINES, run_external_engine
from btc_puzzle_lab.hits import Hit, append_hit, utc_now
from btc_puzzle_lab.paths import scan_checkpoint_path
from btc_puzzle_lab.runlog import log_event

# Soft cap for pure-Python sequential full-range scans (local engine default).
MAX_SEQUENTIAL_KEYS = 2_000_000
DEFAULT_CHUNK_SIZE = 65_536


@dataclass(frozen=True)
class SearchOutcome:
    hit: Hit | None
    engine: str
    message: str
    duplicate: bool = False
    coverage: CoverageLedger | None = None
    chunks_scanned: int = 0


@dataclass(frozen=True)
class ScanCheckpoint:
    puzzle_id: int
    engine: str
    next_secret: int
    end: int
    updated_at: str

    def to_dict(self) -> dict:
        return {
            "puzzle_id": self.puzzle_id,
            "engine": self.engine,
            "next_secret": self.next_secret,
            "end": self.end,
            "next_secret_hex": f"{self.next_secret:x}",
            "end_hex": f"{self.end:x}",
            "updated_at": self.updated_at,
        }


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


def _record_hit(puzzle: Puzzle, secret: int, engine: str) -> SearchOutcome:
    hit = _make_hit(puzzle, secret, engine)
    result = append_hit(hit)
    if result.duplicate:
        log_event(
            "search_duplicate",
            puzzle_id=puzzle.id,
            engine=engine,
            address=hit.address,
        )
        return SearchOutcome(
            hit=hit,
            engine=engine,
            message="hit already recorded (deduped)",
            duplicate=True,
        )
    log_event(
        "search_hit",
        puzzle_id=puzzle.id,
        engine=engine,
        address=hit.address,
        bits=puzzle.bits,
    )
    return SearchOutcome(hit=hit, engine=engine, message="hit recorded")


def load_checkpoint(puzzle_id: int, path: Path | None = None) -> ScanCheckpoint | None:
    target = path or scan_checkpoint_path(puzzle_id)
    if not target.exists():
        return None
    row = json.loads(target.read_text(encoding="utf-8"))
    return ScanCheckpoint(
        puzzle_id=int(row["puzzle_id"]),
        engine=str(row["engine"]),
        next_secret=int(row["next_secret"]),
        end=int(row["end"]),
        updated_at=str(row["updated_at"]),
    )


def save_checkpoint(checkpoint: ScanCheckpoint, path: Path | None = None) -> Path:
    from btc_puzzle_lab.hits import ensure_state_dir

    ensure_state_dir()
    target = path or scan_checkpoint_path(checkpoint.puzzle_id)
    target.write_text(json.dumps(checkpoint.to_dict(), indent=2, sort_keys=True) + "\n")
    os.chmod(target, 0o600)
    return target


def clear_checkpoint(puzzle_id: int, path: Path | None = None) -> None:
    target = path or scan_checkpoint_path(puzzle_id)
    if target.exists():
        target.unlink()


def _progress_printer(puzzle: Puzzle, engine: str, end: int, *, show: bool):
    def _cb(checked: int, secret: int, rate: float) -> None:
        save_checkpoint(
            ScanCheckpoint(
                puzzle_id=puzzle.id,
                engine=engine,
                next_secret=secret + 1,
                end=end,
                updated_at=utc_now(),
            )
        )
        if show:
            remaining = max(end - secret, 0)
            eta = remaining / rate if rate > 0 else 0
            print(
                f"… scanned {checked:,} keys @ {rate:,.0f} keys/s "
                f"(at {secret:x}, eta ~{eta:,.0f}s)",
                flush=True,
            )

    return _cb


def _scan_contiguous(
    puzzle: Puzzle,
    *,
    lo: int,
    hi: int,
    engine: str,
    workers: int,
    progress: bool,
    resume_checkpoint: bool,
) -> int | None:
    """Scan one contiguous inclusive range; returns secret or None."""
    workers = max(1, workers)
    if workers == 1:
        return sequential_find_p2pkh(
            puzzle.address,
            lo,
            hi,
            on_progress=_progress_printer(puzzle, engine, hi, show=progress),
            progress_every=50_000 if (progress or resume_checkpoint) else 0,
        )

    # Workers finish out of order, so the checkpoint may only advance across the
    # contiguous completed prefix. Recording whichever chunk happened to finish
    # last let a later --resume start beyond ranges no worker had scanned yet,
    # silently skipping them.
    completed: dict[int, int] = {}
    frontier = lo

    def on_chunk(chunk_lo: int, chunk_hi: int, found: int | None) -> None:
        nonlocal frontier
        completed[chunk_lo] = chunk_hi
        while frontier in completed:
            frontier = completed.pop(frontier) + 1
        save_checkpoint(
            ScanCheckpoint(
                puzzle_id=puzzle.id,
                engine=engine,
                next_secret=frontier,
                end=hi,
                updated_at=utc_now(),
            )
        )
        if progress:
            status = "HIT" if found is not None else "done"
            print(f"… chunk {chunk_lo:x}:{chunk_hi:x} {status}", flush=True)

    return sequential_find_p2pkh_parallel(
        puzzle.address,
        lo,
        hi,
        workers=workers,
        on_chunk_done=on_chunk,
    )


def _scan_with_coverage(
    puzzle: Puzzle,
    *,
    lo: int,
    hi: int,
    engine: str,
    workers: int = 1,
    progress: bool = True,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    order: str = "sequential",
    seed: int | None = None,
    max_chunks: int | None = None,
) -> SearchOutcome:
    ledger, created = get_or_create_coverage(
        puzzle.id,
        range_start=lo,
        range_end=hi,
        chunk_size=chunk_size,
    )
    if created:
        print(
            f"coverage ledger created puzzle #{puzzle.id} "
            f"chunks={len(ledger.chunks)} size={chunk_size}",
            flush=True,
        )
    else:
        print(format_coverage(ledger), flush=True)

    planned = ledger.plan(order=order, seed=seed, max_chunks=max_chunks)
    if not planned:
        log_event(
            "coverage_complete",
            puzzle_id=puzzle.id,
            engine=engine,
            coverage_ratio=round(ledger.coverage_ratio, 6),
        )
        return SearchOutcome(
            hit=None,
            engine=engine,
            message=(
                f"coverage complete ({ledger.coverage_ratio:.2%} of "
                f"{ledger.total_keys:,} keys); nothing pending"
            ),
            coverage=ledger,
        )

    log_event(
        "search_start",
        puzzle_id=puzzle.id,
        engine=engine,
        mode="coverage",
        order=order,
        seed=seed,
        chunk_size=chunk_size,
        max_chunks=max_chunks,
        planned_chunks=len(planned),
        coverage_ratio=round(ledger.coverage_ratio, 6),
        workers=workers,
    )
    print(
        f"{engine}/coverage puzzle #{puzzle.id} order={order} "
        f"planned={len(planned)} workers={workers}",
        flush=True,
    )

    scanned = 0
    for chunk in planned:
        ledger.mark(chunk.index, "in_progress")
        save_coverage(ledger)
        if progress:
            print(
                f"… coverage chunk #{chunk.index} {chunk.start:x}:{chunk.end:x} "
                f"({chunk.size:,} keys)",
                flush=True,
            )
        secret = _scan_contiguous(
            puzzle,
            lo=chunk.start,
            hi=chunk.end,
            engine=engine,
            workers=workers,
            progress=progress,
            resume_checkpoint=False,
        )
        scanned += 1
        if secret is not None:
            ledger.mark(chunk.index, "hit")
            save_coverage(ledger)
            clear_checkpoint(puzzle.id)
            outcome = _record_hit(puzzle, secret, engine)
            log_event(
                "coverage_hit",
                puzzle_id=puzzle.id,
                chunk_index=chunk.index,
                coverage_ratio=round(ledger.coverage_ratio, 6),
                chunks_scanned=scanned,
            )
            return SearchOutcome(
                hit=outcome.hit,
                engine=engine,
                message=outcome.message,
                duplicate=outcome.duplicate,
                coverage=ledger,
                chunks_scanned=scanned,
            )
        ledger.mark(chunk.index, "done")
        save_coverage(ledger)
        clear_checkpoint(puzzle.id)
        log_event(
            "coverage_chunk_done",
            puzzle_id=puzzle.id,
            chunk_index=chunk.index,
            coverage_ratio=round(ledger.coverage_ratio, 6),
        )

    log_event(
        "search_miss",
        puzzle_id=puzzle.id,
        engine=engine,
        mode="coverage",
        chunks_scanned=scanned,
        coverage_ratio=round(ledger.coverage_ratio, 6),
    )
    pending = ledger.counts()["pending"] + ledger.counts()["in_progress"]
    return SearchOutcome(
        hit=None,
        engine=engine,
        message=(
            f"no match in {scanned} chunk(s); "
            f"coverage={ledger.coverage_ratio:.2%}; pending_chunks={pending}"
        ),
        coverage=ledger,
        chunks_scanned=scanned,
    )


def _scan_range(
    puzzle: Puzzle,
    *,
    lo: int,
    hi: int,
    engine: str,
    workers: int = 1,
    resume: bool = False,
    progress: bool = True,
    enforce_cap: bool = True,
    coverage: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    order: str = "sequential",
    seed: int | None = None,
    max_chunks: int | None = None,
) -> SearchOutcome:
    if coverage:
        if lo > hi:
            return SearchOutcome(hit=None, engine=engine, message="empty scan range")
        # Cap applies to each planned chunk, not the whole puzzle range.
        if enforce_cap and chunk_size > MAX_SEQUENTIAL_KEYS:
            return SearchOutcome(
                hit=None,
                engine=engine,
                message=(
                    f"chunk_size too large for sequential engine ({chunk_size:,} keys). "
                    f"Keep chunk_size <= {MAX_SEQUENTIAL_KEYS:,}."
                ),
            )
        return _scan_with_coverage(
            puzzle,
            lo=lo,
            hi=hi,
            engine=engine,
            workers=workers,
            progress=progress,
            chunk_size=chunk_size,
            order=order,
            seed=seed,
            max_chunks=max_chunks,
        )

    if resume:
        ckpt = load_checkpoint(puzzle.id)
        if ckpt is not None and ckpt.engine == engine and ckpt.end == hi:
            if ckpt.next_secret > hi:
                clear_checkpoint(puzzle.id)
                return SearchOutcome(
                    hit=None,
                    engine=engine,
                    message="checkpoint already past end; cleared",
                )
            lo = max(lo, ckpt.next_secret)
            print(f"resuming puzzle #{puzzle.id} from {lo:x}", flush=True)
    if lo > hi:
        clear_checkpoint(puzzle.id)
        return SearchOutcome(hit=None, engine=engine, message="empty scan range")
    if enforce_cap and hi - lo + 1 > MAX_SEQUENTIAL_KEYS:
        return SearchOutcome(
            hit=None,
            engine=engine,
            message=(
                f"range too large for sequential engine ({hi - lo + 1:,} keys). "
                "Use --window / --coverage for practice, or --engine keyhunt if configured."
            ),
        )
    workers = max(1, workers)
    print(
        f"{engine} scan puzzle #{puzzle.id} range {lo:x}:{hi:x} workers={workers}",
        flush=True,
    )
    log_event(
        "search_start",
        puzzle_id=puzzle.id,
        engine=engine,
        start_hex=f"{lo:x}",
        end_hex=f"{hi:x}",
        workers=workers,
        resume=resume,
    )
    secret = _scan_contiguous(
        puzzle,
        lo=lo,
        hi=hi,
        engine=engine,
        workers=workers,
        progress=progress,
        resume_checkpoint=resume,
    )
    if secret is None:
        clear_checkpoint(puzzle.id)
        log_event("search_miss", puzzle_id=puzzle.id, engine=engine)
        return SearchOutcome(hit=None, engine=engine, message="no match in range")
    clear_checkpoint(puzzle.id)
    return _record_hit(puzzle, secret, engine)


def run_sequential(
    puzzle: Puzzle,
    *,
    start: int | None = None,
    end: int | None = None,
    workers: int = 1,
    resume: bool = False,
    progress: bool = True,
    coverage: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    order: str = "sequential",
    seed: int | None = None,
    max_chunks: int | None = None,
) -> SearchOutcome:
    lo = puzzle.range_start if start is None else start
    hi = puzzle.range_end if end is None else end
    return _scan_range(
        puzzle,
        lo=lo,
        hi=hi,
        engine="sequential",
        workers=workers,
        resume=resume,
        progress=progress,
        enforce_cap=True,
        coverage=coverage,
        chunk_size=chunk_size,
        order=order,
        seed=seed,
        max_chunks=max_chunks,
    )


def run_window(
    puzzle: Puzzle,
    *,
    window: int = 1_000_000,
    workers: int = 1,
    resume: bool = False,
    progress: bool = True,
    coverage: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    order: str = "sequential",
    seed: int | None = None,
    max_chunks: int | None = None,
) -> SearchOutcome:
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
        f"practice window puzzle #{puzzle.id} "
        f"[{lo:x}, {hi:x}] ({hi - lo + 1:,} keys)",
        flush=True,
    )
    return _scan_range(
        puzzle,
        lo=lo,
        hi=hi,
        engine="window",
        workers=workers,
        resume=resume,
        progress=progress,
        enforce_cap=False,
        coverage=coverage,
        chunk_size=chunk_size,
        order=order,
        seed=seed,
        max_chunks=max_chunks,
    )


def run_inject_known(puzzle: Puzzle) -> SearchOutcome:
    """Practice-only: record the catalog solution after local verification."""
    if puzzle.practice_solution is None:
        return SearchOutcome(
            hit=None,
            engine="inject-known",
            message="no practice_solution_hex in catalog",
        )
    return _record_hit(puzzle, puzzle.practice_solution, "inject-known")


def run_external(
    puzzle: Puzzle,
    engine: str,
    *,
    threads: int = 2,
    dp: int = 16,
    timeout: float | None = None,
    progress: bool = True,
) -> SearchOutcome:
    log_event(
        "search_start",
        puzzle_id=puzzle.id,
        engine=engine,
        threads=threads,
        dp=dp,
        timeout=timeout,
    )
    result = run_external_engine(
        puzzle,
        engine,
        threads=threads,
        dp=dp,
        timeout=timeout,
        progress=progress,
    )
    if result.secret is None:
        log_event("search_miss", puzzle_id=puzzle.id, engine=engine, detail=result.message)
        return SearchOutcome(hit=None, engine=engine, message=result.message)
    return _record_hit(puzzle, result.secret, engine)


# Backward-compatible alias used by older tests/docs.
def run_keyhunt(puzzle: Puzzle, *, threads: int = 2) -> SearchOutcome:
    return run_external(puzzle, "keyhunt", threads=threads)


def run_puzzle(
    puzzle: Puzzle,
    *,
    engine: str | None = None,
    window: int = 1_000_000,
    threads: int = 2,
    workers: int = 1,
    resume: bool = False,
    progress: bool = True,
    coverage: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    order: str = "sequential",
    seed: int | None = None,
    max_chunks: int | None = None,
    dp: int = 16,
    timeout: float | None = None,
) -> SearchOutcome:
    choice = (engine or puzzle.engine_default).lower()
    if choice in {"sequential", "seq"}:
        return run_sequential(
            puzzle,
            workers=workers,
            resume=resume,
            progress=progress,
            coverage=coverage,
            chunk_size=chunk_size,
            order=order,
            seed=seed,
            max_chunks=max_chunks,
        )
    if choice in {"window", "practice-window"}:
        return run_window(
            puzzle,
            window=window,
            workers=workers,
            resume=resume,
            progress=progress,
            coverage=coverage,
            chunk_size=chunk_size,
            order=order,
            seed=seed,
            max_chunks=max_chunks,
        )
    if choice in {"inject", "inject-known", "known"}:
        return run_inject_known(puzzle)
    if choice in ENGINES:
        return run_external(
            puzzle,
            choice,
            threads=threads,
            dp=dp,
            timeout=timeout,
            progress=progress,
        )
    return SearchOutcome(hit=None, engine=choice, message=f"unknown engine: {choice}")
