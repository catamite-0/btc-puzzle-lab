"""Catalog-wide automation: plan → batch run → status.

Persists a no-secrets job board in ``state/batch_plan.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from btc_puzzle_lab.catalog import Puzzle, load_puzzles
from btc_puzzle_lab.coverage import load_coverage
from btc_puzzle_lab.engines import resolve_binary
from btc_puzzle_lab.hits import read_hits, utc_now
from btc_puzzle_lab.paths import STATE_DIR
from btc_puzzle_lab.runlog import log_event
from btc_puzzle_lab.search import run_puzzle
from btc_puzzle_lab.strategy import HostProfile, StrategyPlan, plan_strategy, probe_host

JobStatus = Literal[
    "pending",
    "ready",
    "blocked",
    "running",
    "done",
    "hit",
    "skipped",
    "error",
]

LOCAL_ENGINES = frozenset({"sequential", "window", "inject-known"})
EXTERNAL_ENGINES = frozenset({"keyhunt", "bitcrack", "kangaroo", "rckangaroo"})


def batch_plan_path(path: Path | None = None) -> Path:
    return path or (STATE_DIR / "batch_plan.json")


@dataclass
class PuzzleJob:
    puzzle_id: int
    bits: int
    status_catalog: str
    address: str
    has_pubkey: bool
    has_solution: bool
    engine: str
    reason: str
    workers: int = 1
    threads: int = 2
    dp: int = 16
    coverage: bool = False
    chunk_size: int = 65_536
    order: str = "sequential"
    window: int = 1_000_000
    max_chunks: int | None = None
    job_status: JobStatus = "pending"
    blocker: str | None = None
    last_message: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> PuzzleJob:
        return cls(**row)


@dataclass
class BatchPlan:
    created_at: str
    updated_at: str
    source: str
    host: dict[str, Any]
    filters: dict[str, Any]
    jobs: list[PuzzleJob] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source": self.source,
            "host": self.host,
            "filters": self.filters,
            "jobs": [job.to_dict() for job in self.jobs],
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> BatchPlan:
        return cls(
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source=row.get("source", ""),
            host=row.get("host", {}),
            filters=row.get("filters", {}),
            jobs=[PuzzleJob.from_dict(item) for item in row.get("jobs", [])],
        )


def _ensure_state() -> Path:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return STATE_DIR


def save_plan(plan: BatchPlan, path: Path | None = None) -> Path:
    _ensure_state()
    target = batch_plan_path(path)
    plan.updated_at = utc_now()
    created = not target.exists()
    target.write_text(json.dumps(plan.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if created:
        os.chmod(target, 0o600)
    return target


def load_plan(path: Path | None = None) -> BatchPlan | None:
    target = batch_plan_path(path)
    if not target.is_file():
        return None
    return BatchPlan.from_dict(json.loads(target.read_text(encoding="utf-8")))


def _classify_job(plan: StrategyPlan, puzzle: Puzzle) -> tuple[JobStatus, str | None]:
    if plan.engine in LOCAL_ENGINES:
        if plan.engine == "inject-known" and puzzle.practice_solution is None:
            return "blocked", "inject-known needs practice_solution_hex"
        return "ready", None
    if plan.engine in EXTERNAL_ENGINES:
        if resolve_binary(plan.engine) is None:
            if plan.engine in {"keyhunt", "kangaroo"}:
                return (
                    "blocked",
                    f"{plan.engine} binary not found; run: btc-puzzle-lab engines install",
                )
            env = {
                "bitcrack": "BITCRACK_PATH",
                "rckangaroo": "RCKANGAROO_PATH",
            }[plan.engine]
            return "blocked", f"{plan.engine} binary not found; set {env}"
        if plan.engine in {"kangaroo", "rckangaroo"} and not puzzle.pubkey_compressed_hex:
            return "blocked", f"{plan.engine} needs pubkey_compressed_hex"
        return "ready", None
    return "blocked", f"unsupported engine: {plan.engine}"


def _match_filters(
    puzzle: Puzzle,
    *,
    status: str,
    bits_min: int | None,
    bits_max: int | None,
    ids: set[int] | None,
) -> bool:
    if ids is not None and puzzle.id not in ids:
        return False
    if status != "all" and puzzle.status != status:
        return False
    if bits_min is not None and puzzle.bits < bits_min:
        return False
    if bits_max is not None and puzzle.bits > bits_max:
        return False
    return True


def build_plan(
    *,
    status: str = "all",
    bits_min: int | None = None,
    bits_max: int | None = None,
    puzzle_ids: list[int] | None = None,
    host: HostProfile | None = None,
    puzzles: list[Puzzle] | None = None,
) -> BatchPlan:
    profile = host or probe_host()
    catalog = puzzles if puzzles is not None else load_puzzles()
    id_filter = set(puzzle_ids) if puzzle_ids else None
    jobs: list[PuzzleJob] = []
    for puzzle in catalog:
        if not _match_filters(
            puzzle,
            status=status,
            bits_min=bits_min,
            bits_max=bits_max,
            ids=id_filter,
        ):
            continue
        strategy = plan_strategy(puzzle, host=profile)
        job_status, blocker = _classify_job(strategy, puzzle)
        jobs.append(
            PuzzleJob(
                puzzle_id=puzzle.id,
                bits=puzzle.bits,
                status_catalog=puzzle.status,
                address=puzzle.address,
                has_pubkey=bool(puzzle.pubkey_compressed_hex),
                has_solution=puzzle.practice_solution is not None,
                engine=strategy.engine,
                reason=strategy.reason,
                workers=strategy.workers,
                threads=strategy.threads,
                dp=strategy.dp,
                coverage=strategy.coverage,
                chunk_size=strategy.chunk_size,
                order=strategy.order,
                window=strategy.window,
                max_chunks=strategy.max_chunks,
                job_status=job_status,
                blocker=blocker,
                updated_at=utc_now(),
            )
        )
    jobs.sort(key=lambda job: job.puzzle_id)
    now = utc_now()
    return BatchPlan(
        created_at=now,
        updated_at=now,
        source="plan_strategy",
        host=profile.to_dict(),
        filters={
            "status": status,
            "bits_min": bits_min,
            "bits_max": bits_max,
            "puzzle_ids": puzzle_ids or [],
        },
        jobs=jobs,
    )


@dataclass(frozen=True)
class BatchRunResult:
    attempted: int
    hits: int
    done: int
    errors: int
    skipped: int
    stopped_early: bool
    plan_path: Path


def run_batch(
    plan: BatchPlan,
    *,
    limit: int | None = None,
    resume: bool = True,
    stop_on_hit: bool = False,
    include_blocked: bool = False,
    progress: bool = True,
    plan_path: Path | None = None,
) -> BatchRunResult:
    from btc_puzzle_lab.catalog import get_puzzle

    catalog_by_id = {p.id: p for p in load_puzzles()}
    hit_ids = {h.puzzle_id for h in read_hits()}
    attempted = hits = done = errors = skipped = 0
    stopped_early = False

    runnable = []
    for job in plan.jobs:
        if job.puzzle_id in hit_ids:
            if job.job_status != "hit":
                job.job_status = "hit"
                job.last_message = "already in HITS.jsonl"
                job.updated_at = utc_now()
            continue
        if job.job_status in {"hit", "done"} and resume:
            continue
        if job.job_status == "blocked" and not include_blocked:
            skipped += 1
            continue
        if job.job_status == "blocked" and include_blocked:
            # Still blocked at runtime unless binary appeared.
            status, blocker = _classify_job(
                StrategyPlan(
                    engine=job.engine,
                    reason=job.reason,
                    workers=job.workers,
                    threads=job.threads,
                    dp=job.dp,
                    coverage=job.coverage,
                    chunk_size=job.chunk_size,
                    order=job.order,
                    window=job.window,
                    max_chunks=job.max_chunks,
                ),
                catalog_by_id[job.puzzle_id],
            )
            if status == "blocked":
                job.blocker = blocker
                job.updated_at = utc_now()
                skipped += 1
                continue
            job.job_status = "ready"
            job.blocker = None
        runnable.append(job)

    log_event(
        "batch_start",
        jobs=len(plan.jobs),
        runnable=len(runnable),
        limit=limit,
        resume=resume,
        stop_on_hit=stop_on_hit,
    )

    for job in runnable:
        if limit is not None and attempted >= limit:
            stopped_early = True
            break
        puzzle = catalog_by_id.get(job.puzzle_id) or get_puzzle(job.puzzle_id)
        job.job_status = "running"
        job.updated_at = utc_now()
        save_plan(plan, plan_path)
        attempted += 1
        try:
            outcome = run_puzzle(
                puzzle,
                engine=job.engine,
                window=job.window,
                threads=job.threads,
                workers=job.workers,
                resume=True,
                progress=progress,
                coverage=job.coverage,
                chunk_size=job.chunk_size,
                order=job.order,
                max_chunks=job.max_chunks,
                dp=job.dp,
            )
        except Exception as exc:  # noqa: BLE001 — batch continues
            errors += 1
            job.job_status = "error"
            job.last_message = str(exc)
            job.updated_at = utc_now()
            log_event("batch_job_error", puzzle_id=job.puzzle_id, engine=job.engine, error=str(exc))
            save_plan(plan, plan_path)
            continue

        job.last_message = outcome.message
        job.updated_at = utc_now()
        if outcome.hit is not None:
            hits += 1
            job.job_status = "hit"
            log_event(
                "batch_job_hit",
                puzzle_id=job.puzzle_id,
                engine=outcome.engine,
                address=outcome.hit.address,
            )
            save_plan(plan, plan_path)
            if stop_on_hit:
                stopped_early = True
                break
        else:
            done += 1
            job.job_status = "done"
            log_event(
                "batch_job_done",
                puzzle_id=job.puzzle_id,
                engine=outcome.engine,
                message=outcome.message,
            )
            save_plan(plan, plan_path)

    target = save_plan(plan, plan_path)
    log_event(
        "batch_complete",
        attempted=attempted,
        hits=hits,
        done=done,
        errors=errors,
        skipped=skipped,
        stopped_early=stopped_early,
    )
    return BatchRunResult(
        attempted=attempted,
        hits=hits,
        done=done,
        errors=errors,
        skipped=skipped,
        stopped_early=stopped_early,
        plan_path=target,
    )


def format_plan(plan: BatchPlan, *, verbose: bool = False) -> str:
    counts: dict[str, int] = {}
    for job in plan.jobs:
        counts[job.job_status] = counts.get(job.job_status, 0) + 1
    lines = [
        f"batch plan jobs={len(plan.jobs)} updated={plan.updated_at}",
        f"host tier={plan.host.get('tier', '?')} cpus={plan.host.get('cpus')} "
        f"mem_mb={plan.host.get('mem_mb')} gpu={plan.host.get('gpu')} "
        f"engines={','.join(plan.host.get('engines') or []) or '(none)'}",
        f"filters={plan.filters}",
        "status: "
        + ", ".join(f"{name}={counts[name]}" for name in sorted(counts)),
    ]
    if verbose:
        lines.append(
            f"{'ID':>4} {'bits':>4} {'job':<8} {'engine':<12} {'cat':<8} blocker/reason"
        )
        for job in plan.jobs:
            detail = job.blocker or job.reason
            lines.append(
                f"{job.puzzle_id:>4} {job.bits:>4} {job.job_status:<8} "
                f"{job.engine:<12} {job.status_catalog:<8} {detail}"
            )
    return "\n".join(lines)


def format_status(plan: BatchPlan | None = None) -> str:
    plan = plan or load_plan()
    if plan is None:
        return "no batch plan at state/batch_plan.json (run: btc-puzzle-lab plan)"
    hit_ids = {h.puzzle_id for h in read_hits()}
    lines = [
        format_plan(plan, verbose=False),
        "",
        f"{'ID':>4} {'bits':>4} {'job':<8} {'engine':<12} {'cov':>7} hit blocker",
    ]
    for job in plan.jobs:
        ledger = load_coverage(job.puzzle_id)
        cov = f"{ledger.coverage_ratio:6.2%}" if ledger is not None else "   n/a"
        hit = "yes" if job.puzzle_id in hit_ids or job.job_status == "hit" else "no"
        blocker = job.blocker or ""
        lines.append(
            f"{job.puzzle_id:>4} {job.bits:>4} {job.job_status:<8} "
            f"{job.engine:<12} {cov} {hit:<3} {blocker}"
        )
    return "\n".join(lines)
