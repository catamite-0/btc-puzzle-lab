"""Full-loop orchestrator: sync → plan → search → audit → optional sweep.

Single-machine default: one primary puzzle at a time (GPU slot exclusive).
Transfer stays behind existing AUTO_TRANSFER_* safety gates (disabled + dry-run).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from btc_puzzle_lab.audit import AuditResult, fetch_balance_sats, verify_hit
from btc_puzzle_lab.batch import (
    BatchPlan,
    BatchRunResult,
    PuzzleJob,
    batch_plan_path,
    build_plan,
    run_batch,
    save_plan,
)
from btc_puzzle_lab.catalog_import import ImportResult, import_catalog
from btc_puzzle_lab.doctor import doctor_ok, run_doctor
from btc_puzzle_lab.engines import resolve_binary
from btc_puzzle_lab.hits import Hit, read_hits
from btc_puzzle_lab.notify import NotifyResult, format_notify_results, notify_hit
from btc_puzzle_lab.runlog import log_event
from btc_puzzle_lab.strategy import HostProfile, probe_host
from btc_puzzle_lab.transfer import TransferResult, sweep_hit

ResourceFilter = Literal["auto", "cpu", "gpu", "any"]


@dataclass(frozen=True)
class LoopResult:
    host_tier: str
    resource: str
    sync: ImportResult | None
    plan_path: Path
    selected_ids: list[int]
    batch: BatchRunResult | None
    audits: list[AuditResult] = field(default_factory=list)
    transfers: list[TransferResult] = field(default_factory=list)
    notifications: list[NotifyResult] = field(default_factory=list)
    message: str = ""

    @property
    def hits(self) -> int:
        return 0 if self.batch is None else self.batch.hits

    @property
    def ok(self) -> bool:
        if self.batch is not None and self.batch.errors:
            return False
        if any(not item.address_ok or item.error for item in self.audits):
            return False
        if any(item.status == "error" for item in self.transfers):
            return False
        return True


def resolve_resource_filter(
    requested: ResourceFilter,
    host: HostProfile,
) -> Literal["cpu", "gpu", "any"]:
    if requested == "auto":
        return "gpu" if host.gpu or host.tier in {"gpu", "compute"} else "cpu"
    return requested


def select_ready_jobs(
    plan: BatchPlan,
    *,
    resource: Literal["cpu", "gpu", "any"],
    limit: int,
) -> list[PuzzleJob]:
    """Pick ready jobs for one machine: lowest bits first, resource-filtered."""
    ready = [
        job
        for job in plan.jobs
        if job.job_status == "ready"
        and (resource == "any" or job.resource == resource)
    ]
    ready.sort(key=lambda job: (job.bits, job.puzzle_id))
    if limit < 1:
        return []
    return ready[:limit]


def _hits_for_ids(puzzle_ids: set[int]) -> list[Hit]:
    return [hit for hit in read_hits() if hit.puzzle_id in puzzle_ids]


def run_once(
    *,
    sync: bool = False,
    status: str = "solved",
    bits_min: int | None = 1,
    bits_max: int | None = None,
    puzzle_ids: list[int] | None = None,
    limit: int = 1,
    stop_on_hit: bool = True,
    resource: ResourceFilter = "auto",
    require_doctor: bool = True,
    audit: bool = True,
    check_balance: bool = False,
    transfer: bool = False,
    notify: bool = False,
    progress: bool = True,
    timeout: float | None = None,
    plan_path: Path | None = None,
    host: HostProfile | None = None,
) -> LoopResult:
    """Run one closed-loop pass on this host.

    Defaults stay inside the packaged solved-practice catalog. Catalog sync,
    notifications, and transfer are explicit operator opt-ins.
    """
    profile = host or probe_host()
    if require_doctor and not doctor_ok(run_doctor()):
        raise RuntimeError("doctor reported blocking issues; fix then retry `once`")

    resolved = resolve_resource_filter(resource, profile)
    if resolved == "gpu" and resolve_binary("bitcrack") is None and resolve_binary(
        "rckangaroo"
    ) is None:
        raise RuntimeError(
            "GPU slot selected but no GPU solver is installed "
            "(run: btc-puzzle-lab engines install --only bitcrack)"
        )

    sync_result: ImportResult | None = None
    if sync:
        sync_result = import_catalog()
        log_event(
            "loop_sync",
            count=sync_result.count,
            unsolved=sync_result.unsolved,
            source=sync_result.source,
        )

    plan = build_plan(
        status=status,
        bits_min=bits_min,
        bits_max=bits_max,
        puzzle_ids=puzzle_ids,
        host=profile,
    )
    target = plan_path or batch_plan_path()
    save_plan(plan, target)

    selected = select_ready_jobs(plan, resource=resolved, limit=limit)
    selected_ids = [job.puzzle_id for job in selected]
    if not selected_ids:
        msg = (
            f"no ready {resolved} jobs "
            f"(status={status} bits_min={bits_min} bits_max={bits_max})"
        )
        log_event("loop_idle", resource=resolved, message=msg)
        return LoopResult(
            host_tier=profile.tier,
            resource=resolved,
            sync=sync_result,
            plan_path=target,
            selected_ids=[],
            batch=None,
            message=msg,
        )

    # Rebuild a focused board so this host occupies one resource slot only.
    focus = set(selected_ids)
    plan = build_plan(
        status=status,
        bits_min=bits_min,
        bits_max=bits_max,
        puzzle_ids=selected_ids,
        host=profile,
    )
    save_plan(plan, target)

    before_hits = {hit.puzzle_id for hit in read_hits()}
    batch_result = run_batch(
        plan,
        limit=limit,
        resume=True,
        stop_on_hit=stop_on_hit,
        include_blocked=False,
        progress=progress,
        plan_path=target,
        timeout=timeout,
    )

    audits: list[AuditResult] = []
    transfers: list[TransferResult] = []
    notifications: list[NotifyResult] = []
    new_hit_ids = {
        hit.puzzle_id
        for hit in read_hits()
        if hit.puzzle_id in focus and hit.puzzle_id not in before_hits
    } | {
        job.puzzle_id
        for job in plan.jobs
        if job.puzzle_id in focus and job.job_status == "hit"
    }
    transfer_by_id: dict[int, TransferResult] = {}
    if new_hit_ids:
        for hit in _hits_for_ids(new_hit_ids):
            result: AuditResult | None = None
            if audit:
                result = verify_hit(hit)
                if check_balance and result.address_ok and not result.error:
                    try:
                        balance = fetch_balance_sats(hit.address)
                        result = AuditResult(
                            hit=result.hit,
                            address_ok=result.address_ok,
                            derived_address=result.derived_address,
                            balance_sats=balance,
                            addr_type=result.addr_type,
                        )
                    except Exception as exc:  # noqa: BLE001 — surface in audit row
                        result = AuditResult(
                            hit=result.hit,
                            address_ok=result.address_ok,
                            derived_address=result.derived_address,
                            balance_sats=None,
                            error=f"balance lookup failed: {exc}",
                            addr_type=result.addr_type,
                        )
                audits.append(result)
                if transfer and result.address_ok and not result.error:
                    tr = sweep_hit(hit)
                    transfers.append(tr)
                    transfer_by_id[hit.puzzle_id] = tr
            if notify:
                notifications.extend(
                    notify_hit(
                        hit,
                        audit=result,
                        transfer=transfer_by_id.get(hit.puzzle_id),
                    )
                )

    message = (
        f"selected={selected_ids} attempted={batch_result.attempted} "
        f"hits={batch_result.hits} audits={len(audits)} transfers={len(transfers)} "
        f"notifications={len(notifications)}"
    )
    log_event(
        "loop_complete",
        resource=resolved,
        selected=selected_ids,
        hits=batch_result.hits,
        audits=len(audits),
        transfers=len(transfers),
        notifications=len(notifications),
    )
    return LoopResult(
        host_tier=profile.tier,
        resource=resolved,
        sync=sync_result,
        plan_path=target,
        selected_ids=selected_ids,
        batch=batch_result,
        audits=audits,
        transfers=transfers,
        notifications=notifications,
        message=message,
    )


def format_loop_result(result: LoopResult) -> str:
    lines = [
        f"once loop tier={result.host_tier} resource={result.resource}",
        f"plan={result.plan_path}",
    ]
    if result.sync is not None:
        lines.append(
            f"sync puzzles={result.sync.count} unsolved={result.sync.unsolved} "
            f"source={result.sync.source}"
        )
    lines.append(f"selected={result.selected_ids or '(none)'}")
    if result.batch is not None:
        lines.append(
            f"batch attempted={result.batch.attempted} hits={result.batch.hits} "
            f"done={result.batch.done} errors={result.batch.errors} "
            f"stopped_early={result.batch.stopped_early}"
        )
    for item in result.audits:
        mark = "ok" if item.address_ok and not item.error else "fail"
        bal = f" balance_sats={item.balance_sats}" if item.balance_sats is not None else ""
        err = f" error={item.error}" if item.error else ""
        lines.append(f"audit[{mark}] puzzle={item.hit.puzzle_id}{bal}{err}")
    for item in result.transfers:
        lines.append(f"transfer[{item.status}]: {item.message}")
    if result.notifications:
        lines.append(format_notify_results(result.notifications))
    lines.append(result.message)
    return "\n".join(lines)


@dataclass(frozen=True)
class WatchResult:
    passes: int
    hits: int
    stopped_reason: str
    last: LoopResult | None
    results: tuple[LoopResult, ...] = ()


def run_watch(
    *,
    max_hours: float | None = None,
    max_passes: int | None = None,
    idle_sleep: float = 30.0,
    sync_every: int = 1,
    stop_on_hit: bool = True,
    timeout: float | None = None,
    sync: bool = False,
    status: str = "solved",
    bits_min: int | None = 1,
    bits_max: int | None = None,
    puzzle_ids: list[int] | None = None,
    limit: int = 1,
    resource: ResourceFilter = "auto",
    require_doctor: bool = True,
    audit: bool = True,
    check_balance: bool = False,
    transfer: bool = False,
    notify: bool = False,
    progress: bool = True,
    plan_path: Path | None = None,
    host: HostProfile | None = None,
) -> WatchResult:
    """Repeat ``run_once`` until hit, budget, or idle exhaustion.

    This general loop defaults to solved practice entries. Use the dedicated
    bounded synthetic benchmark for paid GPU throughput and resume checks.
    """
    deadline = (
        time.monotonic() + max_hours * 3600.0
        if max_hours is not None and max_hours > 0
        else None
    )
    passes = 0
    hits = 0
    collected: list[LoopResult] = []
    reason = "completed"
    while True:
        if max_passes is not None and passes >= max_passes:
            reason = "max_passes"
            break
        if deadline is not None and time.monotonic() >= deadline:
            reason = "max_hours"
            break

        pass_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "max_hours"
                break
            pass_timeout = remaining if pass_timeout is None else min(pass_timeout, remaining)

        do_sync = sync and (sync_every <= 1 or passes % sync_every == 0)
        result = run_once(
            sync=do_sync,
            status=status,
            bits_min=bits_min,
            bits_max=bits_max,
            puzzle_ids=puzzle_ids,
            limit=limit,
            stop_on_hit=stop_on_hit,
            resource=resource,
            require_doctor=require_doctor,
            audit=audit,
            check_balance=check_balance,
            transfer=transfer,
            notify=notify,
            progress=progress,
            timeout=pass_timeout,
            plan_path=plan_path,
            host=host,
        )
        collected.append(result)
        passes += 1
        hits += result.hits
        log_event(
            "watch_pass",
            pass_no=passes,
            hits=result.hits,
            selected=result.selected_ids,
            message=result.message,
        )
        if result.hits and stop_on_hit:
            reason = "hit"
            break
        if not result.selected_ids:
            if deadline is None and max_passes is None:
                reason = "idle"
                break
            time.sleep(max(0.0, idle_sleep))
            continue
        # External engine finished without a hit (or timed out): brief pause
        # then claim the same slot again unless budgets say stop.
        time.sleep(max(0.0, min(idle_sleep, 5.0)))

    log_event("watch_complete", passes=passes, hits=hits, reason=reason)
    return WatchResult(
        passes=passes,
        hits=hits,
        stopped_reason=reason,
        last=collected[-1] if collected else None,
        results=tuple(collected),
    )


def format_watch_result(result: WatchResult) -> str:
    lines = [
        f"watch passes={result.passes} hits={result.hits} stopped={result.stopped_reason}",
    ]
    if result.last is not None:
        lines.append(format_loop_result(result.last))
    return "\n".join(lines)
