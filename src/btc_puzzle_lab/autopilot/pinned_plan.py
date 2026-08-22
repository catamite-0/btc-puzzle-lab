"""Read-only orchestration and reporting for one explicitly pinned target.

This module is the narrow plan-only boundary for ``auto <id> --plan``.  It
loads the package catalog, binds exactly one id, observes the
physical host, collects selection-purpose chain evidence, and delegates the
pure choice to :func:`plan_target`.  The returned report is descriptive only:
it cannot be used as an execution plan and deliberately contains no private
key, public-key bytes, transaction ids, transaction hex, provider URLs, or
credentials.

Host and chain I/O live behind injected ports.  Importing this module performs
no discovery, network access, configuration writes, catalog synchronization,
toolchain work, or process launch.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fractions import Fraction
from typing import Protocol

from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogSnapshot,
    CatalogTargetBinding,
    is_catalog_snapshot_issued,
    is_catalog_target_binding_issued,
    load_snapshot,
)
from btc_puzzle_lab.autopilot.chain import (
    ChainAdmissionReceipt,
    ChainEvidence,
    ChainEvidenceProvenance,
    PracticeLookupBypass,
    is_chain_admission_receipt_issued,
    is_practice_lookup_bypass_issued,
    is_production_chain_admission_receipt_issued,
)
from btc_puzzle_lab.autopilot.facts import (
    ChainPurpose,
    ChainState,
    HostCapabilities,
)
from btc_puzzle_lab.autopilot.planning import (
    AlgorithmAssessment,
    BitCrackRecipeV1,
    Blocker,
    ExactEstimate,
    KangarooRecipeV1,
    KeyhuntRecipeV1,
    PlanningPolicy,
    PlanningResult,
    SequentialRecipeV1,
    plan_target,
)


class PinnedPlanStage(StrEnum):
    REQUEST = "request"
    CATALOG_LOAD = "catalog_load"
    CATALOG_BIND = "catalog_bind"
    HOST_DISCOVERY = "host_discovery"
    CHAIN_COLLECTION = "chain_collection"
    CLOCK = "clock"
    SELECTION = "selection"


class PinnedPlanErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_PORTS = "invalid_ports"
    CATALOG_LOAD_FAILED = "catalog_load_failed"
    CATALOG_BIND_FAILED = "catalog_bind_failed"
    HOST_DISCOVERY_FAILED = "host_discovery_failed"
    CHAIN_COLLECTION_FAILED = "chain_collection_failed"
    CLOCK_FAILED = "clock_failed"
    SELECTION_FAILED = "selection_failed"
    PORT_CONTRACT_VIOLATION = "port_contract_violation"


class PinnedPlanError(RuntimeError):
    """Typed, non-sensitive acquisition/configuration failure for a CLI."""

    def __init__(
        self,
        *,
        stage: PinnedPlanStage,
        code: PinnedPlanErrorCode,
        detail: str,
        remedy: str,
    ) -> None:
        if type(stage) is not PinnedPlanStage or type(code) is not PinnedPlanErrorCode:
            raise TypeError("pinned plan errors require typed stage and code")
        if any(type(value) is not str or not value for value in (detail, remedy)):
            raise TypeError("pinned plan error detail and remedy must be non-empty text")
        super().__init__(f"{code.value}: {detail}")
        self.stage = stage
        self.code = code
        self.detail = detail
        self.remedy = remedy


class CatalogLoader(Protocol):
    def __call__(self) -> CatalogSnapshot: ...


class CatalogBinder(Protocol):
    def __call__(
        self,
        snapshot: CatalogSnapshot,
        puzzle_id: int,
        /,
    ) -> CatalogTargetBinding: ...


class HostDiscoverer(Protocol):
    def __call__(self) -> HostCapabilities: ...


class SelectionEvidenceCollector(Protocol):
    def __call__(
        self,
        *,
        binding: CatalogTargetBinding,
        purpose: ChainPurpose,
        clock: Callable[[], datetime],
    ) -> ChainEvidence: ...


def _bind_catalog_target(snapshot: CatalogSnapshot, puzzle_id: int) -> CatalogTargetBinding:
    return snapshot.bind_target(puzzle_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class PinnedPlanPorts:
    """All effectful operations needed by the plan-only orchestrator."""

    discover_host: HostDiscoverer
    collect_chain: SelectionEvidenceCollector
    clock: Callable[[], datetime]
    load_catalog: CatalogLoader = load_snapshot
    bind_target: CatalogBinder = _bind_catalog_target
    require_production_chain_receipt: bool = False

    def __post_init__(self) -> None:
        names = (
            "load_catalog",
            "bind_target",
            "discover_host",
            "collect_chain",
            "clock",
        )
        if any(not callable(getattr(self, name)) for name in names):
            raise PinnedPlanError(
                stage=PinnedPlanStage.REQUEST,
                code=PinnedPlanErrorCode.INVALID_PORTS,
                detail="every pinned-plan port must be callable",
                remedy="construct ports with the production factory or typed test adapters",
            )
        if type(self.require_production_chain_receipt) is not bool:
            raise PinnedPlanError(
                stage=PinnedPlanStage.REQUEST,
                code=PinnedPlanErrorCode.INVALID_PORTS,
                detail="production chain receipt requirement must be a boolean",
                remedy="construct ports with the production factory or typed test adapters",
            )


class PinnedPlanOutcome(StrEnum):
    SELECTED = "selected"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class RationalReport:
    numerator: int
    denominator: int

    @classmethod
    def from_fraction(cls, value: Fraction) -> RationalReport:
        return cls(numerator=value.numerator, denominator=value.denominator)

    def render(self) -> str:
        return f"{self.numerator}/{self.denominator}"


@dataclass(frozen=True, slots=True)
class BlockerReport:
    code: str
    detail: str
    remedy: str


type ReportScalar = str | int | bool | None


@dataclass(frozen=True, slots=True)
class RecipeReport:
    kind: str
    adapter_version: int
    parameters: tuple[tuple[str, ReportScalar], ...]


@dataclass(frozen=True, slots=True)
class EstimateReport:
    model_version: str
    source: str
    source_fingerprint: str
    work_unit: str
    full_work: int | None
    horizon_work_limit: int
    full_solution_expected_work: RationalReport
    horizon_expected_occupied_work: RationalReport | None
    horizon_hit_probability: RationalReport | None
    assumed_rate_per_second: int
    confidence: RationalReport
    full_solution_eta_seconds: RationalReport
    horizon_expected_occupied_seconds: RationalReport | None


@dataclass(frozen=True, slots=True)
class AlgorithmReport:
    engine: str
    resource: str
    viable: bool
    provisioning: str
    restart: str
    exact_checkpoint: bool
    recipe: RecipeReport | None
    estimate: EstimateReport | None
    required_host_memory_floor_bytes: int | None
    required_device_memory_floor_bytes: int | None
    explanation: str
    blockers: tuple[BlockerReport, ...]


@dataclass(frozen=True, slots=True)
class TargetReport:
    puzzle_id: int
    mode: str
    bits_label: int | None
    range_size: int
    has_public_key: bool


@dataclass(frozen=True, slots=True)
class GpuReport:
    device_id: str
    name: str
    memory_bytes: int
    compute_capability: str | None
    multiprocessor_count: int | None


@dataclass(frozen=True, slots=True)
class HostReport:
    fingerprint: str
    architecture: str
    cpu_count: int
    memory_bytes: int
    disk_free_bytes: int | None
    gpus: tuple[GpuReport, ...]


@dataclass(frozen=True, slots=True)
class ChainReport:
    evidence_kind: str
    provenance: str
    state: str
    confirmed_sats: int | None
    unconfirmed_sats: int | None
    unknown_reason: str | None
    checked_at: str | None
    fresh_until: str | None
    evidence_fingerprint: str


@dataclass(frozen=True, slots=True)
class PolicyReport:
    objective: str
    planning_horizon_seconds: int
    memory_safety_fraction: RationalReport
    cpu_reserved_cores: int
    allow_address_fallback_for_pubkey: bool
    allow_manual_provisioning: bool
    fingerprint: str


@dataclass(frozen=True, slots=True)
class PinnedPlanReport:
    """Detached, non-executable plan explanation safe for text output."""

    schema_version: int
    outcome: PinnedPlanOutcome
    evaluated_at: str
    catalog_fingerprint: str
    target: TargetReport
    chain: ChainReport
    host: HostReport
    policy: PolicyReport
    target_blockers: tuple[BlockerReport, ...]
    algorithms: tuple[AlgorithmReport, ...]
    selected_engine: str | None
    selection_fingerprint: str | None

    @property
    def selected(self) -> bool:
        return self.outcome is PinnedPlanOutcome.SELECTED

    def render_text(self) -> str:
        chain_amounts = (
            "balance=not_applicable"
            if self.chain.evidence_kind == "practice_bypass"
            else (
                "balance=unknown"
                if self.chain.confirmed_sats is None
                else (
                    f"confirmed_sats={self.chain.confirmed_sats} "
                    f"unconfirmed_sats={self.chain.unconfirmed_sats}"
                )
            )
        )
        selected_algorithm = next(
            (item for item in self.algorithms if item.engine == self.selected_engine),
            None,
        )
        lines = [
            (
                f"pinned plan v{self.schema_version}: outcome={self.outcome.value} "
                f"selected={self.selected_engine or 'none'}"
            ),
            (
                f"target: puzzle={self.target.puzzle_id} mode={self.target.mode} "
                f"range_size={self.target.range_size} "
                f"public_key={'yes' if self.target.has_public_key else 'no'}"
            ),
            (
                f"chain: state={self.chain.state} {chain_amounts} "
                f"provenance={self.chain.provenance} "
                f"checked_at={self.chain.checked_at or 'n/a'} "
                f"fresh_until={self.chain.fresh_until or 'n/a'} "
                f"unknown_reason={self.chain.unknown_reason or 'none'} "
                f"evidence={self.chain.evidence_fingerprint}"
            ),
            (
                f"host: architecture={self.host.architecture} cpus={self.host.cpu_count} "
                f"memory={_bytes_text(self.host.memory_bytes)} "
                f"disk_free={_bytes_text(self.host.disk_free_bytes) if self.host.disk_free_bytes is not None else 'unknown'} "
                f"gpus={len(self.host.gpus)} "
                f"fingerprint={self.host.fingerprint}"
            ),
            (
                f"context: catalog={self.catalog_fingerprint} objective={self.policy.objective} "
                f"policy={self.policy.fingerprint} evaluated_at={self.evaluated_at}"
            ),
        ]
        for gpu in self.host.gpus:
            lines.append(
                f"host gpu: id={gpu.device_id} name={json.dumps(gpu.name, ensure_ascii=True)} "
                f"memory={_bytes_text(gpu.memory_bytes)} "
                f"compute_capability={gpu.compute_capability or 'unknown'} "
                f"multiprocessors={gpu.multiprocessor_count or 'unknown'}"
            )
        if self.target_blockers:
            lines.append("target blockers:")
            lines.extend(_render_blocker(blocker, indent="  ") for blocker in self.target_blockers)
        else:
            lines.append("target blockers: none")
        lines.append("algorithms:")
        for algorithm in self.algorithms:
            if algorithm.engine == self.selected_engine:
                status = "SELECTED"
            elif algorithm.blockers:
                status = "blocked: " + ",".join(blocker.code for blocker in algorithm.blockers)
            elif self.target_blockers:
                status = "viable; target blocked"
            else:
                status = "viable; not chosen by objective/tie-break"
            lines.append(
                f"  {algorithm.engine}: {status}; resource={algorithm.resource}; "
                f"provisioning={algorithm.provisioning}"
            )
        if selected_algorithm is not None:
            recipe = selected_algorithm.recipe
            estimate = selected_algorithm.estimate
            lines.append(
                f"selected plan: engine={selected_algorithm.engine} resource={selected_algorithm.resource} "
                f"provisioning={selected_algorithm.provisioning} restart={selected_algorithm.restart}"
            )
            if recipe is None:
                lines.append("  recipe: unavailable")
            else:
                parameters = json.dumps(
                    dict(recipe.parameters),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                lines.append(
                    f"  recipe: {recipe.kind} adapter_v={recipe.adapter_version} "
                    f"parameters={parameters}"
                )
            if estimate is None:
                lines.append("  estimate: unavailable source=none confidence=none")
            else:
                lines.append(
                    f"  estimate: source={estimate.source} model={estimate.model_version} "
                    f"confidence={_probability_text(estimate.confidence)} "
                    f"rate={estimate.assumed_rate_per_second} {estimate.work_unit}/second"
                )
                lines.append(
                    f"  expected_full_eta={_duration_text(estimate.full_solution_eta_seconds)} "
                    f"expected_full_work={estimate.full_solution_expected_work.render()}"
                )
                lines.append(
                    f"  horizon_hit_probability={_optional_probability_text(estimate.horizon_hit_probability)} "
                    f"horizon_expected_occupied_time={_optional_duration_text(estimate.horizon_expected_occupied_seconds)}"
                )
            lines.append(
                "  memory_floor: "
                f"host={_bytes_text(selected_algorithm.required_host_memory_floor_bytes)} "
                f"device={_bytes_text(selected_algorithm.required_device_memory_floor_bytes)}"
            )
            lines.append(f"  note: {selected_algorithm.explanation}")
            lines.append(f"selection fingerprint: {self.selection_fingerprint}")
        else:
            lines.append("selected plan: none")
        lines.append(
            "limits: Preparation=not_run; baseline=versioned assumption, not measured on this host"
        )
        return "\n".join(lines)


def _render_blocker(blocker: BlockerReport, *, indent: str) -> str:
    return f"{indent}[{blocker.code}] {blocker.detail} remedy={blocker.remedy}"


def _bytes_text(value: int | None) -> str:
    if value is None:
        return "unknown(Preparation gate)"
    units = ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB"))
    for divisor, label in units:
        if value >= divisor:
            whole, remainder = divmod(value, divisor)
            tenth = remainder * 10 // divisor
            return f"{value}B(~{whole}.{tenth}{label})"
    return f"{value}B"


def _probability_text(value: RationalReport) -> str:
    basis_points = value.numerator * 10_000 // value.denominator
    if value.numerator > 0 and basis_points == 0:
        approximate = "<0.01%"
    else:
        approximate = f"~{basis_points // 100}.{basis_points % 100:02d}%"
    return f"{value.render()}({approximate})"


def _optional_probability_text(value: RationalReport | None) -> str:
    return _probability_text(value) if value is not None else "unknown"


def _duration_text(value: RationalReport) -> str:
    if value.numerator < value.denominator:
        return f"{value.render()}s(<1s)"
    seconds = (value.numerator + value.denominator - 1) // value.denominator
    units = (
        (31_557_600, "years"),
        (86_400, "days"),
        (3_600, "hours"),
        (60, "minutes"),
    )
    for divisor, label in units:
        if seconds >= divisor:
            whole, remainder = divmod(seconds, divisor)
            tenth = remainder * 10 // divisor
            return f"{value.render()}s(~{whole}.{tenth} {label};ceil={seconds}s)"
    return f"{value.render()}s(~{seconds}s)"


def _optional_duration_text(value: RationalReport | None) -> str:
    return _duration_text(value) if value is not None else "unknown"


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _blocker_report(blocker: Blocker) -> BlockerReport:
    return BlockerReport(
        code=blocker.code.value,
        detail=blocker.detail,
        remedy=blocker.remedy,
    )


def _ranges_summary(ranges: tuple[object, ...]) -> tuple[tuple[str, ReportScalar], ...]:
    return (
        ("match_material", "address"),
        ("remaining_range_count", len(ranges)),
        ("remaining_key_count", sum(item.size for item in ranges)),
    )


def _recipe_report(assessment: AlgorithmAssessment) -> RecipeReport | None:
    recipe = assessment.recipe
    if recipe is None:
        return None
    if type(recipe) is SequentialRecipeV1:
        return RecipeReport(
            kind="sequential_v1",
            adapter_version=recipe.adapter_version,
            parameters=_ranges_summary(recipe.remaining_ranges),
        )
    if type(recipe) is KeyhuntRecipeV1:
        return RecipeReport(
            kind="keyhunt_v1",
            adapter_version=recipe.adapter_version,
            parameters=_ranges_summary(recipe.remaining_ranges),
        )
    if type(recipe) is BitCrackRecipeV1:
        return RecipeReport(
            kind="bitcrack_v1",
            adapter_version=recipe.adapter_version,
            parameters=_ranges_summary(recipe.remaining_ranges)
            + (("device_id", recipe.device_id),),
        )
    if type(recipe) is KangarooRecipeV1:
        return RecipeReport(
            kind="kangaroo_v1",
            adapter_version=recipe.adapter_version,
            parameters=(
                ("match_material", "public_key"),
                ("range_size", recipe.range_end - recipe.range_start + 1),
                ("range_exponent", recipe.range_exponent),
                ("device_id", recipe.device_id),
                ("distinguished_point_bits_min", recipe.distinguished_point_bits_min),
                ("distinguished_point_bits_max", recipe.distinguished_point_bits_max),
            ),
        )
    raise PinnedPlanError(
        stage=PinnedPlanStage.SELECTION,
        code=PinnedPlanErrorCode.SELECTION_FAILED,
        detail="the planner returned an unsupported recipe type",
        remedy="update the pinned-plan report adapter before exposing the new recipe",
    )


def _rational(value: Fraction | None) -> RationalReport | None:
    return RationalReport.from_fraction(value) if value is not None else None


def _estimate_report(estimate: ExactEstimate | None) -> EstimateReport | None:
    if estimate is None:
        return None
    return EstimateReport(
        model_version=estimate.model_version,
        source=estimate.source.value,
        source_fingerprint=estimate.source_fingerprint,
        work_unit=estimate.work_unit,
        full_work=estimate.full_work,
        horizon_work_limit=estimate.horizon_work_limit,
        full_solution_expected_work=RationalReport.from_fraction(
            estimate.full_solution_expected_work
        ),
        horizon_expected_occupied_work=_rational(estimate.horizon_expected_occupied_work),
        horizon_hit_probability=_rational(estimate.horizon_hit_probability),
        assumed_rate_per_second=estimate.assumed_rate_per_second,
        confidence=RationalReport.from_fraction(estimate.confidence),
        full_solution_eta_seconds=RationalReport.from_fraction(estimate.full_solution_eta_seconds),
        horizon_expected_occupied_seconds=_rational(estimate.horizon_expected_occupied_seconds),
    )


def _algorithm_report(assessment: AlgorithmAssessment) -> AlgorithmReport:
    return AlgorithmReport(
        engine=assessment.engine.value,
        resource=assessment.resource.value,
        viable=assessment.viable,
        provisioning=assessment.provisioning.value,
        restart=assessment.restart.value,
        exact_checkpoint=assessment.exact_checkpoint,
        recipe=_recipe_report(assessment),
        estimate=_estimate_report(assessment.estimate),
        required_host_memory_floor_bytes=assessment.required_host_memory_floor_bytes,
        required_device_memory_floor_bytes=assessment.required_device_memory_floor_bytes,
        explanation=assessment.explanation,
        blockers=tuple(_blocker_report(blocker) for blocker in assessment.blockers),
    )


def _chain_report(evidence: ChainEvidence) -> ChainReport:
    if type(evidence) is PracticeLookupBypass:
        return ChainReport(
            evidence_kind="practice_bypass",
            provenance=evidence.provenance.value,
            state="PRACTICE",
            confirmed_sats=None,
            unconfirmed_sats=None,
            unknown_reason=None,
            checked_at=None,
            fresh_until=None,
            evidence_fingerprint=evidence.receipt_fingerprint,
        )
    if type(evidence) is not ChainAdmissionReceipt:
        raise PinnedPlanError(
            stage=PinnedPlanStage.CHAIN_COLLECTION,
            code=PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION,
            detail="chain collector returned an unsupported evidence type",
            remedy="use the registered selection-evidence collector",
        )
    snapshot = evidence.snapshot
    known = snapshot.state is not ChainState.UNKNOWN
    return ChainReport(
        evidence_kind="chain_receipt",
        provenance=evidence.provenance.value,
        state=snapshot.state.value,
        confirmed_sats=snapshot.confirmed_sats if known else None,
        unconfirmed_sats=snapshot.unconfirmed_sats if known else None,
        unknown_reason=snapshot.unknown_reason,
        checked_at=_utc_text(snapshot.checked_at),
        fresh_until=_utc_text(snapshot.fresh_until),
        evidence_fingerprint=evidence.receipt_fingerprint,
    )


def _report(
    *,
    binding: CatalogTargetBinding,
    evidence: ChainEvidence,
    host: HostCapabilities,
    policy: PlanningPolicy,
    evaluated_at: datetime,
    result: PlanningResult,
) -> PinnedPlanReport:
    target = binding.target
    decision = result.decision
    return PinnedPlanReport(
        schema_version=1,
        outcome=(PinnedPlanOutcome.SELECTED if decision is not None else PinnedPlanOutcome.BLOCKED),
        evaluated_at=_utc_text(evaluated_at),
        catalog_fingerprint=binding.catalog_fingerprint,
        target=TargetReport(
            puzzle_id=target.puzzle_id,
            mode=target.mode.value,
            bits_label=target.bits_label,
            range_size=target.range_size,
            has_public_key=target.has_public_key,
        ),
        chain=_chain_report(evidence),
        host=HostReport(
            fingerprint=host.fingerprint,
            architecture=host.architecture,
            cpu_count=host.cpu_count,
            memory_bytes=host.memory_bytes,
            disk_free_bytes=host.disk_free_bytes,
            gpus=tuple(
                GpuReport(
                    device_id=gpu.device_id,
                    name=gpu.name,
                    memory_bytes=gpu.memory_bytes,
                    compute_capability=(
                        ".".join(str(part) for part in gpu.compute_capability)
                        if gpu.compute_capability
                        else None
                    ),
                    multiprocessor_count=gpu.multiprocessor_count,
                )
                for gpu in host.gpus
            ),
        ),
        policy=PolicyReport(
            objective=policy.objective,
            planning_horizon_seconds=policy.planning_horizon_seconds,
            memory_safety_fraction=RationalReport.from_fraction(policy.memory_safety_fraction),
            cpu_reserved_cores=policy.cpu_reserved_cores,
            allow_address_fallback_for_pubkey=policy.allow_address_fallback_for_pubkey,
            allow_manual_provisioning=policy.allow_manual_provisioning,
            fingerprint=policy.policy_fingerprint,
        ),
        target_blockers=tuple(_blocker_report(blocker) for blocker in result.target_blockers),
        algorithms=tuple(_algorithm_report(assessment) for assessment in result.assessments),
        selected_engine=decision.selected.engine.value if decision else None,
        selection_fingerprint=decision.decision_fingerprint if decision else None,
    )


def _dependency_failure(
    *,
    stage: PinnedPlanStage,
    code: PinnedPlanErrorCode,
    operation: str,
    remedy: str,
    cause: Exception,
) -> PinnedPlanError:
    return PinnedPlanError(
        stage=stage,
        code=code,
        detail=f"{operation} failed ({type(cause).__name__})",
        remedy=remedy,
    )


def _host_discovery_failure(cause: Exception) -> PinnedPlanError:
    """Map only locally typed host failures to static, non-sensitive guidance."""

    from btc_puzzle_lab.autopilot.host import HostDiscoveryCode, HostDiscoveryError

    if type(cause) is HostDiscoveryError and cause.code is HostDiscoveryCode.NVIDIA_PROBE_FAILED:
        return PinnedPlanError(
            stage=PinnedPlanStage.HOST_DISCOVERY,
            code=PinnedPlanErrorCode.HOST_DISCOVERY_FAILED,
            detail="physical host discovery failed (nvidia_probe_failed)",
            remedy=(
                "in Preparation, use MIG-aware discovery for MIG slices; otherwise repair "
                "GPU visibility and nvidia-smi, then retry"
            ),
        )
    return _dependency_failure(
        stage=PinnedPlanStage.HOST_DISCOVERY,
        code=PinnedPlanErrorCode.HOST_DISCOVERY_FAILED,
        operation="physical host discovery",
        remedy="repair access to physical CPU, memory, disk, and GPU facts",
        cause=cause,
    )


def _read_clock(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception as exc:  # noqa: BLE001 - trusted port failure becomes a typed CLI error
        raise _dependency_failure(
            stage=PinnedPlanStage.CLOCK,
            code=PinnedPlanErrorCode.CLOCK_FAILED,
            operation="planning clock",
            remedy="provide a working timezone-aware UTC clock",
            cause=exc,
        ) from exc
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PinnedPlanError(
            stage=PinnedPlanStage.CLOCK,
            code=PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION,
            detail="planning clock did not return a timezone-aware datetime",
            remedy="repair the production clock adapter",
        )
    return value


def build_pinned_plan(
    puzzle_id: int,
    *,
    ports: PinnedPlanPorts,
    policy: PlanningPolicy | None = None,
) -> PinnedPlanReport:
    """Build one non-executable plan report in a fixed, read-only sequence."""

    if type(puzzle_id) is not int or puzzle_id < 1:
        raise PinnedPlanError(
            stage=PinnedPlanStage.REQUEST,
            code=PinnedPlanErrorCode.INVALID_REQUEST,
            detail="puzzle id must be a positive integer",
            remedy="choose an id from the package catalog",
        )
    if type(ports) is not PinnedPlanPorts:
        raise PinnedPlanError(
            stage=PinnedPlanStage.REQUEST,
            code=PinnedPlanErrorCode.INVALID_PORTS,
            detail="ports must be a PinnedPlanPorts value",
            remedy="construct ports with the production factory or typed test adapters",
        )
    if policy is None:
        chosen_policy = PlanningPolicy()
    elif type(policy) is PlanningPolicy:
        chosen_policy = policy
    else:
        raise PinnedPlanError(
            stage=PinnedPlanStage.REQUEST,
            code=PinnedPlanErrorCode.INVALID_REQUEST,
            detail="policy must be a PlanningPolicy value",
            remedy="use the default policy or construct a validated planning policy",
        )

    try:
        snapshot = ports.load_catalog()
    except Exception as exc:  # noqa: BLE001 - port failures become typed CLI errors
        raise _dependency_failure(
            stage=PinnedPlanStage.CATALOG_LOAD,
            code=PinnedPlanErrorCode.CATALOG_LOAD_FAILED,
            operation="package catalog load",
            remedy="repair or reinstall the package-owned catalog",
            cause=exc,
        ) from exc
    if type(snapshot) is not CatalogSnapshot or not is_catalog_snapshot_issued(snapshot):
        raise PinnedPlanError(
            stage=PinnedPlanStage.CATALOG_LOAD,
            code=PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION,
            detail="catalog loader did not return an issued immutable snapshot",
            remedy="use the package catalog loader",
        )

    try:
        binding = ports.bind_target(snapshot, puzzle_id)
    except Exception as exc:  # noqa: BLE001 - port failures become typed CLI errors
        raise _dependency_failure(
            stage=PinnedPlanStage.CATALOG_BIND,
            code=PinnedPlanErrorCode.CATALOG_BIND_FAILED,
            operation=f"binding puzzle {puzzle_id}",
            remedy="choose an id present in the installed package catalog",
            cause=exc,
        ) from exc
    if (
        type(binding) is not CatalogTargetBinding
        or not is_catalog_target_binding_issued(binding)
        or binding.target.puzzle_id != puzzle_id
        or binding.catalog_fingerprint != snapshot.catalog_fingerprint
    ):
        raise PinnedPlanError(
            stage=PinnedPlanStage.CATALOG_BIND,
            code=PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION,
            detail="catalog binder did not bind the requested id to the loaded snapshot",
            remedy="use the snapshot target binder",
        )

    try:
        host = ports.discover_host()
    except Exception as exc:  # noqa: BLE001 - port failures become typed CLI errors
        raise _host_discovery_failure(exc) from exc
    if type(host) is not HostCapabilities:
        raise PinnedPlanError(
            stage=PinnedPlanStage.HOST_DISCOVERY,
            code=PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION,
            detail="host discoverer did not return HostCapabilities",
            remedy="use the inventory-blind physical host adapter",
        )

    try:
        evidence = ports.collect_chain(
            binding=binding,
            purpose=ChainPurpose.SELECTION,
            clock=ports.clock,
        )
    except Exception as exc:  # noqa: BLE001 - port failures become typed CLI errors
        raise _dependency_failure(
            stage=PinnedPlanStage.CHAIN_COLLECTION,
            code=PinnedPlanErrorCode.CHAIN_COLLECTION_FAILED,
            operation="selection-purpose chain collection",
            remedy="restore the configured provider quorum and retry",
            cause=exc,
        ) from exc
    valid_live_evidence = (
        type(evidence) is ChainAdmissionReceipt
        and is_chain_admission_receipt_issued(evidence)
        and evidence.target == binding.target
        and evidence.snapshot.purpose is ChainPurpose.SELECTION
        and (
            not ports.require_production_chain_receipt
            or is_production_chain_admission_receipt_issued(evidence)
        )
    )
    valid_practice_evidence = (
        type(evidence) is PracticeLookupBypass
        and is_practice_lookup_bypass_issued(evidence)
        and evidence.target == binding.target
        and evidence.purpose is ChainPurpose.SELECTION
        and evidence.provenance is ChainEvidenceProvenance.CATALOG_PRACTICE_V1
    )
    if not (valid_live_evidence or valid_practice_evidence):
        raise PinnedPlanError(
            stage=PinnedPlanStage.CHAIN_COLLECTION,
            code=PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION,
            detail="chain collector did not return unchanged evidence for this selection target",
            remedy="use the production selection-evidence collector",
        )

    evaluated_at = _read_clock(ports.clock)
    try:
        result = plan_target(
            binding,
            evidence,
            host,
            evaluated_at=evaluated_at,
            policy=chosen_policy,
        )
    except Exception as exc:  # noqa: BLE001 - selection failures become typed CLI errors
        raise _dependency_failure(
            stage=PinnedPlanStage.SELECTION,
            code=PinnedPlanErrorCode.SELECTION_FAILED,
            operation="pure algorithm selection",
            remedy="inspect catalog, chain, host, and planning policy compatibility",
            cause=exc,
        ) from exc
    if type(result) is not PlanningResult:
        raise PinnedPlanError(
            stage=PinnedPlanStage.SELECTION,
            code=PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION,
            detail="planner did not return a PlanningResult",
            remedy="repair the planning adapter contract",
        )
    return _report(
        binding=binding,
        evidence=evidence,
        host=host,
        policy=chosen_policy,
        evaluated_at=evaluated_at,
        result=result,
    )


def production_pinned_plan_ports() -> PinnedPlanPorts:
    """Construct production ports without making the CLI assemble adapters.

    Host and public-chain adapters are imported lazily so importing the
    plan-only reporting module remains inert.  The adapter modules own their
    physical probes, provider registry, transport limits, and clock use.
    """

    try:
        from btc_puzzle_lab.autopilot.chain import (
            collect_production_chain_evidence,
        )
        from btc_puzzle_lab.autopilot.host import discover_host
    except ImportError as exc:
        raise PinnedPlanError(
            stage=PinnedPlanStage.REQUEST,
            code=PinnedPlanErrorCode.INVALID_PORTS,
            detail="production host or chain adapter is not available",
            remedy="install a build containing the production plan-only adapters",
        ) from exc

    def collect_chain(
        *,
        binding: CatalogTargetBinding,
        purpose: ChainPurpose,
        clock: Callable[[], datetime],
    ) -> ChainEvidence:
        del clock
        return collect_production_chain_evidence(
            target=binding.target,
            purpose=purpose,
            practice_fixture=binding.practice_fixture,
        )

    return PinnedPlanPorts(
        discover_host=discover_host,
        collect_chain=collect_chain,
        clock=lambda: datetime.now(UTC),
        require_production_chain_receipt=True,
    )


__all__ = [
    "AlgorithmReport",
    "BlockerReport",
    "ChainReport",
    "EstimateReport",
    "HostReport",
    "PinnedPlanError",
    "PinnedPlanErrorCode",
    "PinnedPlanOutcome",
    "PinnedPlanPorts",
    "PinnedPlanReport",
    "PinnedPlanStage",
    "PolicyReport",
    "RecipeReport",
    "TargetReport",
    "build_pinned_plan",
    "production_pinned_plan_ports",
]
