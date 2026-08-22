"""Read-only orchestration and detached reporting for catalog-wide preview.

The production path has four fixed stages: load the sealed package catalog,
discover the effective host, issue the static fastest ranking, then collect the
bounded production chain prefix.  This module never provisions, builds, runs,
notifies, signs, or transfers anything.

Reports contain only non-sensitive primitive summaries.  They deliberately do
not retain catalog bindings, chain receipts, selection decisions, addresses,
public keys, transaction identifiers, or algorithm recipes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from btc_puzzle_lab.autopilot.catalog_ranking import (
    CATALOG_FASTEST_OBJECTIVE_V1,
    CatalogFastestRankingReceipt,
    ExactRational,
    is_catalog_fastest_ranking_receipt_issued,
    rank_catalog_fastest,
)
from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogSnapshot,
    CatalogSnapshotProvenance,
    is_packaged_catalog_snapshot_issued,
    load_snapshot,
)
from btc_puzzle_lab.autopilot.chain import (
    CatalogChainBatchOutcome,
    CatalogChainBatchReceipt,
    ChainEvidenceProvenance,
    collect_production_catalog_prefix,
    is_catalog_chain_batch_receipt_issued,
)
from btc_puzzle_lab.autopilot.facts import ChainPurpose, HostCapabilities, TargetMode
from btc_puzzle_lab.autopilot.host import discover_host
from btc_puzzle_lab.autopilot.planning import PlanningPolicy


class CatalogPreviewStage(StrEnum):
    REQUEST = "request"
    CATALOG = "catalog"
    HOST = "host"
    RANKING = "ranking"
    CHAIN = "chain"
    REPORT = "report"


class CatalogPreviewErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    CATALOG_LOAD_FAILED = "catalog_load_failed"
    CATALOG_AUTHORITY_INVALID = "catalog_authority_invalid"
    HOST_DISCOVERY_FAILED = "host_discovery_failed"
    HOST_FACTS_INVALID = "host_facts_invalid"
    RANKING_FAILED = "ranking_failed"
    RANKING_AUTHORITY_INVALID = "ranking_authority_invalid"
    CHAIN_COLLECTION_FAILED = "chain_collection_failed"
    CHAIN_BATCH_INVALID = "chain_batch_invalid"
    REPORT_CONTRACT_VIOLATION = "report_contract_violation"


class CatalogPreviewError(RuntimeError):
    """Stable non-sensitive failure from one preview stage."""

    def __init__(
        self,
        *,
        stage: CatalogPreviewStage,
        code: CatalogPreviewErrorCode,
        detail: str,
        remedy: str,
    ) -> None:
        if type(stage) is not CatalogPreviewStage or type(code) is not CatalogPreviewErrorCode:
            raise TypeError("catalog preview errors require typed stage and code values")
        if any(type(value) is not str or not value for value in (detail, remedy)):
            raise TypeError("catalog preview errors require non-empty detail and remedy")
        self.stage = stage
        self.code = code
        self.detail = detail
        self.remedy = remedy
        super().__init__(f"{stage.value}:{code.value}: {detail}")


type CatalogLoader = Callable[[], CatalogSnapshot]
type HostDiscoverer = Callable[[], HostCapabilities]
type CatalogRanker = Callable[
    [CatalogSnapshot, HostCapabilities, PlanningPolicy], CatalogFastestRankingReceipt
]
type PrefixCollector = Callable[[CatalogFastestRankingReceipt], CatalogChainBatchReceipt]
type PreviewClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogPreviewPorts:
    """Explicit dependencies for the read-only four-stage workflow."""

    load_catalog: CatalogLoader
    discover_host: HostDiscoverer
    rank_catalog: CatalogRanker
    collect_prefix: PrefixCollector
    clock: PreviewClock

    def __post_init__(self) -> None:
        if any(
            not callable(port)
            for port in (
                self.load_catalog,
                self.discover_host,
                self.rank_catalog,
                self.collect_prefix,
                self.clock,
            )
        ):
            raise TypeError("catalog preview ports must all be callable")


class CatalogPreviewOutcome(StrEnum):
    SELECTED = "selected"
    INDETERMINATE = "indeterminate"
    NO_CONFIRMED_SELECTABLE_TARGET = "no_confirmed_selectable_target"


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogPreviewRational:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if type(self.numerator) is not int or type(self.denominator) is not int:
            raise TypeError("preview rational components must be integers")
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("preview rational components must be positive")

    @classmethod
    def from_ranking(cls, value: ExactRational) -> CatalogPreviewRational:
        if type(value) is not ExactRational:
            raise TypeError("ranking rational has an unsupported type")
        return cls(numerator=value.numerator, denominator=value.denominator)

    def render(self) -> str:
        return (
            str(self.numerator) if self.denominator == 1 else f"{self.numerator}/{self.denominator}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogScopeSummary:
    total_count: int
    live_count: int
    practice_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogComparisonSummary:
    objective: str
    estimate_basis: str
    confidence: str
    policy_fingerprint: str
    economic_optimum: str
    balanced_optimum: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogHostSummary:
    architecture: str
    effective_cpu_count: int
    effective_memory_bytes: int
    disk_free_bytes: int | None
    gpu_count: int
    gpu_memory_bytes_total: int
    host_fingerprint: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogRankingSummary:
    candidate_count: int
    algorithmically_selectable_count: int
    statically_blocked_count: int
    ranking_fingerprint: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogBatchSummary:
    provenance: str
    batch_fingerprint: str
    started_at: str
    completed_at: str
    prefix_min_fresh_until: str | None
    checked_count: int
    not_checked_count: int
    request_count: int
    decompressed_bytes: int
    unique_transaction_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogPrefixCandidateSummary:
    rank: int
    puzzle_id: int
    engine: str
    status: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogPrefixSummary:
    checked: tuple[CatalogPrefixCandidateSummary, ...]
    stop_reason: str
    terminal_rank: int | None
    terminal_puzzle_id: int | None
    terminal_engine: str | None
    terminal_status: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogSelectedSummary:
    catalog_rank: int
    puzzle_id: int
    engine: str
    full_solution_eta_seconds: CatalogPreviewRational
    estimate_confidence: CatalogPreviewRational
    confirmed_sats: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogPreviewReport:
    """Detached non-executable summary of one catalog preview."""

    schema_version: int
    outcome: CatalogPreviewOutcome
    catalog_fingerprint: str
    catalog_provenance: str
    scope: CatalogScopeSummary
    comparison: CatalogComparisonSummary
    host: CatalogHostSummary
    ranking: CatalogRankingSummary
    batch: CatalogBatchSummary
    prefix: CatalogPrefixSummary
    selected: CatalogSelectedSummary | None
    authority: str
    preparation: str
    execution_feasibility: str

    def render_text(self) -> str:
        status_counts = Counter(candidate.status for candidate in self.prefix.checked)
        statuses = ",".join(f"{status}:{status_counts[status]}" for status in sorted(status_counts))
        terminal = (
            (
                f"rank={self.prefix.terminal_rank} puzzle=#{self.prefix.terminal_puzzle_id} "
                f"engine={self.prefix.terminal_engine} status={self.prefix.terminal_status}"
            )
            if self.prefix.terminal_puzzle_id is not None
            else "none"
        )
        selected = (
            (
                f"selected rank={self.selected.catalog_rank} puzzle=#{self.selected.puzzle_id} "
                f"engine={self.selected.engine} "
                f"full_eta_seconds={self.selected.full_solution_eta_seconds.render()} "
                f"confidence={self.selected.estimate_confidence.render()} "
                f"confirmed_sats={self.selected.confirmed_sats}"
            )
            if self.selected
            else "selected none"
        )
        lines = (
            f"catalog-preview outcome={self.outcome.value}",
            (
                f"scope total={self.scope.total_count} live={self.scope.live_count} "
                f"practice={self.scope.practice_count}"
            ),
            (
                f"comparison objective={self.comparison.objective} "
                f"basis={self.comparison.estimate_basis} confidence={self.comparison.confidence} "
                f"policy={self.comparison.policy_fingerprint}"
            ),
            "economic/balanced optimum: not claimed",
            (
                f"host arch={self.host.architecture} cpus={self.host.effective_cpu_count} "
                f"memory_bytes={self.host.effective_memory_bytes} gpus={self.host.gpu_count}"
            ),
            (
                f"ranking selectable={self.ranking.algorithmically_selectable_count} "
                f"blocked={self.ranking.statically_blocked_count}"
            ),
            (
                f"batch provenance={self.batch.provenance} checked={self.batch.checked_count} "
                f"not_checked={self.batch.not_checked_count} requests={self.batch.request_count} "
                f"bytes={self.batch.decompressed_bytes} "
                f"window={self.batch.started_at}..{self.batch.completed_at} "
                f"fresh_until={self.batch.prefix_min_fresh_until or 'n/a'}"
            ),
            (
                f"fingerprints ranking={self.ranking.ranking_fingerprint} "
                f"batch={self.batch.batch_fingerprint}"
            ),
            (
                f"prefix stop_reason={self.prefix.stop_reason} terminal={terminal} "
                f"statuses={statuses or 'none'}"
            ),
            selected,
            f"Preparation: {self.preparation}",
            f"Execution feasibility: {self.execution_feasibility}",
        )
        return "\n".join(lines)


def production_catalog_preview_ports() -> CatalogPreviewPorts:
    """Return direct production dependencies without running any stage."""

    return CatalogPreviewPorts(
        load_catalog=load_snapshot,
        discover_host=discover_host,
        rank_catalog=rank_catalog_fastest,
        collect_prefix=collect_production_catalog_prefix,
        clock=_utc_now,
    )


def _preview_error(
    stage: CatalogPreviewStage,
    code: CatalogPreviewErrorCode,
    detail: str,
    remedy: str,
) -> CatalogPreviewError:
    return CatalogPreviewError(stage=stage, code=code, detail=detail, remedy=remedy)


def _validate_ports(ports: object) -> CatalogPreviewPorts:
    if type(ports) is not CatalogPreviewPorts:
        raise _preview_error(
            CatalogPreviewStage.REQUEST,
            CatalogPreviewErrorCode.INVALID_REQUEST,
            "preview ports are missing or unsupported",
            "use production_catalog_preview_ports or an exact test port bundle",
        )
    try:
        valid = all(
            callable(port)
            for port in (
                ports.load_catalog,
                ports.discover_host,
                ports.rank_catalog,
                ports.collect_prefix,
                ports.clock,
            )
        )
    except Exception:
        valid = False
    if not valid:
        raise _preview_error(
            CatalogPreviewStage.REQUEST,
            CatalogPreviewErrorCode.INVALID_REQUEST,
            "preview ports are incomplete",
            "provide all four read-only stage callables",
        )
    return ports


def _load_catalog(ports: CatalogPreviewPorts) -> CatalogSnapshot:
    try:
        snapshot = ports.load_catalog()
    except Exception:
        raise _preview_error(
            CatalogPreviewStage.CATALOG,
            CatalogPreviewErrorCode.CATALOG_LOAD_FAILED,
            "the package catalog could not be loaded",
            "verify the installed package catalog and retry",
        ) from None
    if type(snapshot) is not CatalogSnapshot or not is_packaged_catalog_snapshot_issued(snapshot):
        raise _preview_error(
            CatalogPreviewStage.CATALOG,
            CatalogPreviewErrorCode.CATALOG_AUTHORITY_INVALID,
            "catalog loading did not return sealed package authority",
            "reload the package-owned catalog without a custom path",
        )
    return snapshot


def _discover_host(ports: CatalogPreviewPorts) -> HostCapabilities:
    try:
        host = ports.discover_host()
    except Exception:
        raise _preview_error(
            CatalogPreviewStage.HOST,
            CatalogPreviewErrorCode.HOST_DISCOVERY_FAILED,
            "effective host discovery failed",
            "repair the local host probes and retry",
        ) from None
    if type(host) is not HostCapabilities:
        raise _preview_error(
            CatalogPreviewStage.HOST,
            CatalogPreviewErrorCode.HOST_FACTS_INVALID,
            "host discovery returned unsupported facts",
            "rediscover the effective host with the built-in adapter",
        )
    return host


def _rank_catalog(
    ports: CatalogPreviewPorts,
    snapshot: CatalogSnapshot,
    host: HostCapabilities,
    policy: PlanningPolicy,
) -> CatalogFastestRankingReceipt:
    try:
        ranking = ports.rank_catalog(snapshot, host, policy)
    except Exception:
        raise _preview_error(
            CatalogPreviewStage.RANKING,
            CatalogPreviewErrorCode.RANKING_FAILED,
            "static catalog comparison failed",
            "inspect catalog, host, and planning policy compatibility",
        ) from None
    live_ids = tuple(
        entry.target.puzzle_id for entry in snapshot.entries if entry.target.mode is TargetMode.LIVE
    )
    if (
        type(ranking) is not CatalogFastestRankingReceipt
        or not is_catalog_fastest_ranking_receipt_issued(ranking)
        or ranking.catalog_fingerprint != snapshot.catalog_fingerprint
        or ranking.catalog_provenance is not CatalogSnapshotProvenance.PACKAGE_V1
        or ranking.preview_host is not host
        or ranking.host_fingerprint != host.fingerprint
        or ranking.policy_fingerprint != policy.policy_fingerprint
        or ranking.objective != CATALOG_FASTEST_OBJECTIVE_V1
        or ranking.purpose is not ChainPurpose.SELECTION
        or ranking.candidate_ids != live_ids
        or ranking.executable is not False
    ):
        raise _preview_error(
            CatalogPreviewStage.RANKING,
            CatalogPreviewErrorCode.RANKING_AUTHORITY_INVALID,
            "static comparison did not return matching issued authority",
            "rerun the built-in package ranking on the discovered host",
        )
    return ranking


def _collect_prefix(
    ports: CatalogPreviewPorts,
    ranking: CatalogFastestRankingReceipt,
) -> CatalogChainBatchReceipt:
    try:
        batch = ports.collect_prefix(ranking)
    except Exception:
        raise _preview_error(
            CatalogPreviewStage.CHAIN,
            CatalogPreviewErrorCode.CHAIN_COLLECTION_FAILED,
            "bounded production chain collection failed",
            "restore public provider access and retry the preview",
        ) from None
    if (
        type(batch) is not CatalogChainBatchReceipt
        or not is_catalog_chain_batch_receipt_issued(batch)
        or batch.ranking is not ranking
        or batch.catalog_fingerprint != ranking.catalog_fingerprint
        or batch.catalog_provenance is not ranking.catalog_provenance
        or batch.host_fingerprint != ranking.host_fingerprint
        or batch.policy_fingerprint != ranking.policy_fingerprint
        or batch.ranking_fingerprint != ranking.ranking_fingerprint
        or batch.objective != ranking.objective
        or batch.purpose is not ChainPurpose.SELECTION
        or batch.provenance is not ChainEvidenceProvenance.PRODUCTION_CATALOG_HTTP_V1
    ):
        raise _preview_error(
            CatalogPreviewStage.CHAIN,
            CatalogPreviewErrorCode.CHAIN_BATCH_INVALID,
            "chain collection did not return a matching issued production batch",
            "rerun the sealed production prefix collector",
        )
    return batch


def _outcome(batch: CatalogChainBatchReceipt) -> CatalogPreviewOutcome:
    return {
        CatalogChainBatchOutcome.SELECTED: CatalogPreviewOutcome.SELECTED,
        CatalogChainBatchOutcome.INDETERMINATE: CatalogPreviewOutcome.INDETERMINATE,
        CatalogChainBatchOutcome.NO_FEASIBLE: (
            CatalogPreviewOutcome.NO_CONFIRMED_SELECTABLE_TARGET
        ),
    }[batch.outcome]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _freshness_summary(
    batch: CatalogChainBatchReceipt,
    clock: PreviewClock,
) -> str | None:
    try:
        evaluated_at = clock()
    except Exception:
        raise _preview_error(
            CatalogPreviewStage.REPORT,
            CatalogPreviewErrorCode.REPORT_CONTRACT_VIOLATION,
            "the report freshness clock failed",
            "repair the local UTC clock and rerun the preview",
        ) from None
    if (
        type(evaluated_at) is not datetime
        or evaluated_at.tzinfo is None
        or evaluated_at.utcoffset() is None
        or batch.batch_started_at > evaluated_at
        or batch.batch_completed_at > evaluated_at
    ):
        raise _preview_error(
            CatalogPreviewStage.REPORT,
            CatalogPreviewErrorCode.REPORT_CONTRACT_VIOLATION,
            "the production batch is not valid at the report time",
            "refresh chain evidence with a correct UTC clock",
        )
    receipts = batch.prefix_receipts
    if any(
        not receipt.snapshot.is_fresh(evaluated_at, purpose=ChainPurpose.SELECTION)
        for receipt in receipts
    ):
        raise _preview_error(
            CatalogPreviewStage.REPORT,
            CatalogPreviewErrorCode.REPORT_CONTRACT_VIOLATION,
            "the checked production prefix expired before reporting",
            "rerun the catalog preview to refresh chain evidence",
        )
    return min((_utc_text(receipt.snapshot.fresh_until) for receipt in receipts), default=None)


def _build_report(
    snapshot: CatalogSnapshot,
    host: HostCapabilities,
    ranking: CatalogFastestRankingReceipt,
    batch: CatalogChainBatchReceipt,
    clock: PreviewClock,
) -> CatalogPreviewReport:
    prefix_min_fresh_until = _freshness_summary(batch, clock)
    checked = tuple(
        CatalogPrefixCandidateSummary(
            rank=index,
            puzzle_id=batch_candidate.puzzle_id,
            engine=ranked_candidate.selected_for_comparison.engine.value,
            status=batch_candidate.status.value.lower(),
        )
        for index, (batch_candidate, ranked_candidate) in enumerate(
            zip(
                batch.candidates[: batch.checked_count],
                ranking.algorithmically_selectable[: batch.checked_count],
                strict=True,
            ),
            start=1,
        )
    )
    terminal = (
        checked[-1]
        if checked
        and batch.outcome
        in {
            CatalogChainBatchOutcome.SELECTED,
            CatalogChainBatchOutcome.INDETERMINATE,
        }
        else None
    )
    stop_reason = {
        CatalogChainBatchOutcome.SELECTED: "confirmed_funded_candidate",
        CatalogChainBatchOutcome.INDETERMINATE: "unknown_chain_state",
        CatalogChainBatchOutcome.NO_FEASIBLE: (
            "ranked_candidates_exhausted"
            if ranking.algorithmically_selectable
            else "no_algorithmically_selectable_candidates"
        ),
    }[batch.outcome]
    selected = None
    if batch.outcome is CatalogChainBatchOutcome.SELECTED:
        selected_receipt = batch.selected_receipt
        if (
            selected_receipt is None
            or not checked
            or batch.selected_target_id != checked[-1].puzzle_id
            or selected_receipt.snapshot.confirmed_sats <= 0
        ):
            raise _preview_error(
                CatalogPreviewStage.REPORT,
                CatalogPreviewErrorCode.REPORT_CONTRACT_VIOLATION,
                "selected batch lacks a consistent confirmed summary",
                "discard the batch and rerun the preview",
            )
        ranked = ranking.algorithmically_selectable[batch.checked_count - 1]
        comparison = ranked.selected_for_comparison
        selected = CatalogSelectedSummary(
            catalog_rank=batch.checked_count,
            puzzle_id=ranked.puzzle_id,
            engine=comparison.engine.value,
            full_solution_eta_seconds=CatalogPreviewRational.from_ranking(
                comparison.full_solution_eta_seconds
            ),
            estimate_confidence=CatalogPreviewRational.from_ranking(comparison.estimate_confidence),
            confirmed_sats=selected_receipt.snapshot.confirmed_sats,
        )
    elif batch.selected_target_id is not None or batch.selected_receipt is not None:
        raise _preview_error(
            CatalogPreviewStage.REPORT,
            CatalogPreviewErrorCode.REPORT_CONTRACT_VIOLATION,
            "non-selected batch unexpectedly carries a selected target",
            "discard the batch and rerun the preview",
        )

    live_count = len(ranking.candidate_ids)
    return CatalogPreviewReport(
        schema_version=1,
        outcome=_outcome(batch),
        catalog_fingerprint=snapshot.catalog_fingerprint,
        catalog_provenance=snapshot.provenance.value,
        scope=CatalogScopeSummary(
            total_count=len(snapshot.entries),
            live_count=live_count,
            practice_count=len(snapshot.entries) - live_count,
        ),
        comparison=CatalogComparisonSummary(
            objective=ranking.objective,
            estimate_basis="low_confidence_baseline_complete_solution_eta",
            confidence="low",
            policy_fingerprint=ranking.policy_fingerprint,
            economic_optimum="not_claimed",
            balanced_optimum="not_claimed",
        ),
        host=CatalogHostSummary(
            architecture=host.architecture,
            effective_cpu_count=host.cpu_count,
            effective_memory_bytes=host.memory_bytes,
            disk_free_bytes=host.disk_free_bytes,
            gpu_count=len(host.gpus),
            gpu_memory_bytes_total=sum(gpu.memory_bytes for gpu in host.gpus),
            host_fingerprint=host.fingerprint,
        ),
        ranking=CatalogRankingSummary(
            candidate_count=live_count,
            algorithmically_selectable_count=len(ranking.algorithmically_selectable),
            statically_blocked_count=len(ranking.statically_blocked),
            ranking_fingerprint=ranking.ranking_fingerprint,
        ),
        batch=CatalogBatchSummary(
            provenance=batch.provenance.value,
            batch_fingerprint=batch.receipt_fingerprint,
            started_at=_utc_text(batch.batch_started_at),
            completed_at=_utc_text(batch.batch_completed_at),
            prefix_min_fresh_until=prefix_min_fresh_until,
            checked_count=batch.checked_count,
            not_checked_count=len(batch.candidates) - batch.checked_count,
            request_count=batch.request_count,
            decompressed_bytes=batch.decompressed_bytes,
            unique_transaction_count=batch.unique_transaction_count,
        ),
        prefix=CatalogPrefixSummary(
            checked=checked,
            stop_reason=stop_reason,
            terminal_rank=terminal.rank if terminal else None,
            terminal_puzzle_id=terminal.puzzle_id if terminal else None,
            terminal_engine=terminal.engine if terminal else None,
            terminal_status=terminal.status if terminal else None,
        ),
        selected=selected,
        authority="detached",
        preparation="not_run",
        execution_feasibility="not_evaluated",
    )


def build_catalog_preview(
    *,
    ports: CatalogPreviewPorts,
    policy: PlanningPolicy | None = None,
) -> CatalogPreviewReport:
    """Run the fixed read-only catalog preview flow and return a detached report."""

    chosen_ports = _validate_ports(ports)
    if policy is None:
        chosen_policy = PlanningPolicy()
    elif type(policy) is PlanningPolicy:
        chosen_policy = policy
    else:
        raise _preview_error(
            CatalogPreviewStage.REQUEST,
            CatalogPreviewErrorCode.INVALID_REQUEST,
            "planning policy has an unsupported type",
            "provide PlanningPolicy or use the default",
        )

    snapshot = _load_catalog(chosen_ports)
    host = _discover_host(chosen_ports)
    ranking = _rank_catalog(chosen_ports, snapshot, host, chosen_policy)
    batch = _collect_prefix(chosen_ports, ranking)
    return _build_report(snapshot, host, ranking, batch, chosen_ports.clock)


__all__ = [
    "CatalogBatchSummary",
    "CatalogComparisonSummary",
    "CatalogHostSummary",
    "CatalogPrefixCandidateSummary",
    "CatalogPrefixSummary",
    "CatalogPreviewError",
    "CatalogPreviewErrorCode",
    "CatalogPreviewOutcome",
    "CatalogPreviewPorts",
    "CatalogPreviewRational",
    "CatalogPreviewReport",
    "CatalogPreviewStage",
    "CatalogRankingSummary",
    "CatalogScopeSummary",
    "CatalogSelectedSummary",
    "build_catalog_preview",
    "production_catalog_preview_ports",
]
