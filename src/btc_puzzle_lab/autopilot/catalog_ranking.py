"""Pure static ranking of the complete public puzzle catalog.

This module deliberately stops before chain acquisition.  It accepts an exact,
unchanged package-catalog snapshot plus descriptive host and policy facts, then
uses the same algorithm assessments as :func:`planning.plan_target` to produce
a complete order for later bounded chain collection.  It performs no I/O.

The receipt is descriptive and non-executable.  Its ``preview_host`` is an
ordinary DTO that may support a planning preview; Preparation must rediscover
the effective host and issue its own admission before build or execution.  The
receipt is process-local batch input only, not serialized authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction

from btc_puzzle_lab.autopilot._issuance import ProcessLocalIssuance
from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogSnapshot,
    CatalogSnapshotProvenance,
    CatalogTargetBinding,
    is_catalog_snapshot_issued,
    is_catalog_target_binding_issued,
    is_packaged_catalog_snapshot_issued,
)
from btc_puzzle_lab.autopilot.facts import (
    ChainPurpose,
    EngineName,
    HostCapabilities,
    ResourceClass,
    TargetMode,
)
from btc_puzzle_lab.autopilot.planning import (
    AlgorithmAssessment,
    Blocker,
    EstimateSource,
    PlanningPolicy,
    ProvisioningPolicy,
    algorithm_assessment_fingerprint,
    assess_target_algorithms,
    select_algorithm_for_comparison,
)

CATALOG_FASTEST_OBJECTIVE_V1 = "fastest_full_solution_eta_baseline_v1"

_PUBLIC_CATALOG_V1_IDS = tuple(range(1, 161))
_PUBLIC_CATALOG_V1_PRACTICE_COUNT = 82
_PUBLIC_CATALOG_V1_LIVE_COUNT = 78
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RANKING_FACTORY_TOKEN = object()


class CatalogRankingErrorCode(StrEnum):
    """Stable reasons a static catalog ranking request was rejected."""

    INVALID_REQUEST = "invalid_request"
    SNAPSHOT_NOT_ISSUED = "snapshot_not_issued"
    SNAPSHOT_NOT_PACKAGED = "snapshot_not_packaged"
    SNAPSHOT_NOT_COMPLETE = "snapshot_not_complete"
    ASSESSMENT_CONTRACT_VIOLATION = "assessment_contract_violation"


class CatalogRankingValidationError(ValueError):
    """Typed failure before a complete static ranking can be issued."""

    def __init__(self, code: CatalogRankingErrorCode, detail: str) -> None:
        if type(code) is not CatalogRankingErrorCode or type(detail) is not str or not detail:
            raise TypeError("catalog ranking errors require a typed code and non-empty detail")
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ExactRational:
    """JSON-friendly representation of one exact positive rational."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if type(self.numerator) is not int or type(self.denominator) is not int:
            raise TypeError("exact rational components must be integers")
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("exact rational components must be positive")

    @classmethod
    def from_fraction(cls, value: Fraction) -> ExactRational:
        if type(value) is not Fraction or value <= 0:
            raise TypeError("exact rational requires a positive Fraction")
        return cls(value.numerator, value.denominator)

    def as_fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogSelectedForComparison:
    """Detached summary of one target's selected static algorithm assessment."""

    engine: EngineName
    resource: ResourceClass
    provisioning: ProvisioningPolicy
    estimate_model: str
    estimate_source: EstimateSource
    full_solution_eta_seconds: ExactRational
    estimate_confidence: ExactRational
    assessment_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.engine) is not EngineName:
            raise TypeError("engine must be EngineName")
        if type(self.resource) is not ResourceClass:
            raise TypeError("resource must be ResourceClass")
        if type(self.provisioning) is not ProvisioningPolicy:
            raise TypeError("provisioning must be ProvisioningPolicy")
        if (
            type(self.estimate_model) is not str
            or not self.estimate_model
            or self.estimate_model != self.estimate_model.strip()
        ):
            raise ValueError("estimate_model must be non-empty trimmed text")
        if type(self.estimate_source) is not EstimateSource:
            raise TypeError("estimate_source must be EstimateSource")
        if type(self.full_solution_eta_seconds) is not ExactRational:
            raise TypeError("full_solution_eta_seconds must be ExactRational")
        if type(self.estimate_confidence) is not ExactRational:
            raise TypeError("estimate_confidence must be ExactRational")
        confidence = self.estimate_confidence.as_fraction()
        if confidence > 1:
            raise ValueError("estimate_confidence must not exceed one")
        if not isinstance(self.assessment_fingerprint, str) or not _SHA256.fullmatch(
            self.assessment_fingerprint
        ):
            raise ValueError("assessment_fingerprint must be a SHA-256 digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogFastestCandidate:
    """One live target participating in the versioned complete-ETA order."""

    binding: CatalogTargetBinding
    selected_for_comparison: CatalogSelectedForComparison

    def __post_init__(self) -> None:
        if type(self.binding) is not CatalogTargetBinding:
            raise TypeError("binding must be CatalogTargetBinding")
        if self.binding.target.mode is not TargetMode.LIVE:
            raise ValueError("catalog fastest candidates must be live targets")
        if type(self.selected_for_comparison) is not CatalogSelectedForComparison:
            raise TypeError("selected_for_comparison has an unsupported type")

    @property
    def puzzle_id(self) -> int:
        return self.binding.target.puzzle_id

    def order_key(self) -> tuple[Fraction, Fraction, int, str]:
        """ETA asc, confidence desc, puzzle id asc, engine name asc."""

        selected = self.selected_for_comparison
        return (
            selected.full_solution_eta_seconds.as_fraction(),
            -selected.estimate_confidence.as_fraction(),
            self.puzzle_id,
            selected.engine.value,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogStaticAlgorithmBlockers:
    """Static blockers returned for one algorithm family."""

    engine: EngineName
    blockers: tuple[Blocker, ...]

    def __post_init__(self) -> None:
        if type(self.engine) is not EngineName:
            raise TypeError("engine must be EngineName")
        if (
            type(self.blockers) is not tuple
            or not self.blockers
            or any(type(blocker) is not Blocker for blocker in self.blockers)
        ):
            raise TypeError("blockers must be a non-empty exact tuple of Blocker values")


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogStaticBlockedCandidate:
    """One live target for which every static algorithm has a blocker."""

    binding: CatalogTargetBinding
    algorithm_blockers: tuple[CatalogStaticAlgorithmBlockers, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not CatalogTargetBinding:
            raise TypeError("binding must be CatalogTargetBinding")
        if self.binding.target.mode is not TargetMode.LIVE:
            raise ValueError("static blocked candidates must be live targets")
        if type(self.algorithm_blockers) is not tuple or any(
            type(item) is not CatalogStaticAlgorithmBlockers for item in self.algorithm_blockers
        ):
            raise TypeError("algorithm_blockers has an unsupported type")
        if tuple(item.engine for item in self.algorithm_blockers) != tuple(EngineName):
            raise ValueError("algorithm_blockers must cover every engine in canonical order")

    @property
    def puzzle_id(self) -> int:
        return self.binding.target.puzzle_id


def _ranking_fingerprint(
    *,
    catalog_fingerprint: str,
    catalog_provenance: CatalogSnapshotProvenance,
    host_fingerprint: str,
    policy_fingerprint: str,
    objective: str,
    purpose: ChainPurpose,
    candidate_ids: tuple[int, ...],
    algorithmically_selectable: tuple[CatalogFastestCandidate, ...],
    statically_blocked: tuple[CatalogStaticBlockedCandidate, ...],
) -> str:
    payload = {
        "contract_version": 1,
        "catalog_fingerprint": catalog_fingerprint,
        "catalog_provenance": catalog_provenance.value,
        "host_fingerprint": host_fingerprint,
        "policy_fingerprint": policy_fingerprint,
        "objective": objective,
        "purpose": purpose.value,
        "candidate_ids": candidate_ids,
        "algorithmically_selectable": [
            {
                "puzzle_id": candidate.puzzle_id,
                "engine": candidate.selected_for_comparison.engine.value,
                "eta": (
                    candidate.selected_for_comparison.full_solution_eta_seconds.numerator,
                    candidate.selected_for_comparison.full_solution_eta_seconds.denominator,
                ),
                "confidence": (
                    candidate.selected_for_comparison.estimate_confidence.numerator,
                    candidate.selected_for_comparison.estimate_confidence.denominator,
                ),
                "assessment": candidate.selected_for_comparison.assessment_fingerprint,
            }
            for candidate in algorithmically_selectable
        ],
        "statically_blocked": [
            {
                "puzzle_id": candidate.puzzle_id,
                "algorithms": [
                    {
                        "engine": algorithm.engine.value,
                        "blockers": [
                            {
                                "code": blocker.code.value,
                                "detail": blocker.detail,
                                "remedy": blocker.remedy,
                            }
                            for blocker in algorithm.blockers
                        ],
                    }
                    for algorithm in candidate.algorithm_blockers
                ],
            }
            for candidate in statically_blocked
        ],
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class CatalogFastestRankingReceipt:
    """Process-local, non-executable input for bounded live-chain collection."""

    catalog_fingerprint: str
    catalog_provenance: CatalogSnapshotProvenance
    preview_host: HostCapabilities
    host_fingerprint: str
    policy_fingerprint: str
    objective: str
    purpose: ChainPurpose
    candidate_ids: tuple[int, ...]
    algorithmically_selectable: tuple[CatalogFastestCandidate, ...]
    statically_blocked: tuple[CatalogStaticBlockedCandidate, ...]
    ranking_fingerprint: str
    executable: bool = field(init=False)

    def __init__(
        self,
        *,
        catalog_fingerprint: str,
        catalog_provenance: CatalogSnapshotProvenance,
        preview_host: HostCapabilities,
        host_fingerprint: str,
        policy_fingerprint: str,
        objective: str,
        purpose: ChainPurpose,
        candidate_ids: tuple[int, ...],
        algorithmically_selectable: tuple[CatalogFastestCandidate, ...],
        statically_blocked: tuple[CatalogStaticBlockedCandidate, ...],
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _RANKING_FACTORY_TOKEN:
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.INVALID_REQUEST,
                "ranking receipts must come from rank_catalog_fastest",
            )
        if not _SHA256.fullmatch(catalog_fingerprint):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "catalog fingerprint is malformed",
            )
        if catalog_provenance is not CatalogSnapshotProvenance.PACKAGE_V1:
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "catalog provenance must be sealed package-v1",
            )
        if (
            type(preview_host) is not HostCapabilities
            or host_fingerprint != preview_host.fingerprint
        ):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "preview host does not match its fingerprint",
            )
        if not _SHA256.fullmatch(policy_fingerprint):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "policy fingerprint is malformed",
            )
        if objective != CATALOG_FASTEST_OBJECTIVE_V1 or purpose is not ChainPurpose.SELECTION:
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "ranking objective or purpose is unsupported",
            )
        if type(candidate_ids) is not tuple or len(candidate_ids) != _PUBLIC_CATALOG_V1_LIVE_COUNT:
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "ranking must bind every live candidate",
            )
        if type(algorithmically_selectable) is not tuple or any(
            type(item) is not CatalogFastestCandidate for item in algorithmically_selectable
        ):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "algorithmically_selectable has an unsupported type",
            )
        if type(statically_blocked) is not tuple or any(
            type(item) is not CatalogStaticBlockedCandidate for item in statically_blocked
        ):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "statically_blocked has an unsupported type",
            )
        ranked_ids = tuple(item.puzzle_id for item in algorithmically_selectable)
        blocked_ids = tuple(item.puzzle_id for item in statically_blocked)
        if len(set(ranked_ids + blocked_ids)) != len(candidate_ids) or set(
            ranked_ids + blocked_ids
        ) != set(candidate_ids):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "ranked and blocked groups do not cover the complete candidate set exactly once",
            )
        if algorithmically_selectable != tuple(
            sorted(algorithmically_selectable, key=CatalogFastestCandidate.order_key)
        ):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "algorithmically selectable candidates are not in the versioned total order",
            )
        if blocked_ids != tuple(sorted(blocked_ids)):
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                "static blocked candidates must remain in puzzle-id order",
            )
        for candidate in algorithmically_selectable + statically_blocked:
            if (
                not is_catalog_target_binding_issued(candidate.binding)
                or candidate.binding.catalog_fingerprint != catalog_fingerprint
            ):
                raise CatalogRankingValidationError(
                    CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                    f"puzzle {candidate.puzzle_id} lacks an unchanged catalog binding",
                )

        ranking_fingerprint = _ranking_fingerprint(
            catalog_fingerprint=catalog_fingerprint,
            catalog_provenance=catalog_provenance,
            host_fingerprint=host_fingerprint,
            policy_fingerprint=policy_fingerprint,
            objective=objective,
            purpose=purpose,
            candidate_ids=candidate_ids,
            algorithmically_selectable=algorithmically_selectable,
            statically_blocked=statically_blocked,
        )
        object.__setattr__(self, "catalog_fingerprint", catalog_fingerprint)
        object.__setattr__(self, "catalog_provenance", catalog_provenance)
        object.__setattr__(self, "preview_host", preview_host)
        object.__setattr__(self, "host_fingerprint", host_fingerprint)
        object.__setattr__(self, "policy_fingerprint", policy_fingerprint)
        object.__setattr__(self, "objective", objective)
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "candidate_ids", candidate_ids)
        object.__setattr__(self, "algorithmically_selectable", algorithmically_selectable)
        object.__setattr__(self, "statically_blocked", statically_blocked)
        object.__setattr__(self, "ranking_fingerprint", ranking_fingerprint)
        object.__setattr__(self, "executable", False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("CatalogFastestRankingReceipt is final and cannot be subclassed")


_RANKING_ISSUANCE = ProcessLocalIssuance(CatalogFastestRankingReceipt)


def is_catalog_fastest_ranking_receipt_issued(value: object) -> bool:
    """Return whether this exact ranking was issued here and remains unchanged."""

    return _RANKING_ISSUANCE.is_valid(value)


def _validate_complete_snapshot(snapshot: CatalogSnapshot) -> tuple[int, ...]:
    if type(snapshot) is not CatalogSnapshot or not is_catalog_snapshot_issued(snapshot):
        raise CatalogRankingValidationError(
            CatalogRankingErrorCode.SNAPSHOT_NOT_ISSUED,
            "snapshot must be an exact unchanged catalog-issued value",
        )
    if not is_packaged_catalog_snapshot_issued(snapshot):
        raise CatalogRankingValidationError(
            CatalogRankingErrorCode.SNAPSHOT_NOT_PACKAGED,
            "catalog fastest v1 requires the sealed package-owned snapshot",
        )
    target_ids = tuple(entry.target.puzzle_id for entry in snapshot.entries)
    practice_count = sum(entry.target.mode is TargetMode.PRACTICE for entry in snapshot.entries)
    live_ids = tuple(
        entry.target.puzzle_id for entry in snapshot.entries if entry.target.mode is TargetMode.LIVE
    )
    if (
        target_ids != _PUBLIC_CATALOG_V1_IDS
        or practice_count != _PUBLIC_CATALOG_V1_PRACTICE_COUNT
        or len(live_ids) != _PUBLIC_CATALOG_V1_LIVE_COUNT
    ):
        raise CatalogRankingValidationError(
            CatalogRankingErrorCode.SNAPSHOT_NOT_COMPLETE,
            "catalog fastest v1 requires ids 1..160 with 82 practice and 78 live targets",
        )
    return live_ids


def _selected_summary(assessment: AlgorithmAssessment) -> CatalogSelectedForComparison:
    estimate = assessment.estimate
    if estimate is None:
        raise CatalogRankingValidationError(
            CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
            "selected algorithm has no complete baseline estimate",
        )
    return CatalogSelectedForComparison(
        engine=assessment.engine,
        resource=assessment.resource,
        provisioning=assessment.provisioning,
        estimate_model=estimate.model_version,
        estimate_source=estimate.source,
        full_solution_eta_seconds=ExactRational.from_fraction(estimate.full_solution_eta_seconds),
        estimate_confidence=ExactRational.from_fraction(estimate.confidence),
        assessment_fingerprint=algorithm_assessment_fingerprint(assessment),
    )


def rank_catalog_fastest(
    snapshot: CatalogSnapshot,
    host: HostCapabilities,
    policy: PlanningPolicy,
) -> CatalogFastestRankingReceipt:
    """Issue the complete static order for later bounded chain acquisition.

    Practice entries are excluded.  Every live entry appears exactly once in
    either ``algorithmically_selectable`` or ``statically_blocked``.
    """

    live_ids = _validate_complete_snapshot(snapshot)
    if type(host) is not HostCapabilities or type(policy) is not PlanningPolicy:
        raise CatalogRankingValidationError(
            CatalogRankingErrorCode.INVALID_REQUEST,
            "host and policy must be exact typed facts",
        )
    if policy.objective != "fastest":
        raise CatalogRankingValidationError(
            CatalogRankingErrorCode.INVALID_REQUEST,
            "catalog fastest v1 requires the fastest planning policy",
        )

    ranked: list[CatalogFastestCandidate] = []
    blocked: list[CatalogStaticBlockedCandidate] = []
    for puzzle_id in live_ids:
        binding = snapshot.bind_target(puzzle_id)
        assessments = assess_target_algorithms(binding.target, host, policy=policy)
        selected = select_algorithm_for_comparison(assessments)
        if selected is not None:
            ranked.append(
                CatalogFastestCandidate(
                    binding=binding,
                    selected_for_comparison=_selected_summary(selected),
                )
            )
            continue
        try:
            algorithm_blockers = tuple(
                CatalogStaticAlgorithmBlockers(
                    engine=assessment.engine,
                    blockers=assessment.blockers,
                )
                for assessment in assessments
            )
        except (TypeError, ValueError) as exc:
            raise CatalogRankingValidationError(
                CatalogRankingErrorCode.ASSESSMENT_CONTRACT_VIOLATION,
                f"puzzle {puzzle_id} has an incomplete static blocker set",
            ) from exc
        blocked.append(
            CatalogStaticBlockedCandidate(
                binding=binding,
                algorithm_blockers=algorithm_blockers,
            )
        )

    ordered_ranked = tuple(sorted(ranked, key=CatalogFastestCandidate.order_key))
    ordered_blocked = tuple(sorted(blocked, key=lambda candidate: candidate.puzzle_id))
    receipt = CatalogFastestRankingReceipt(
        catalog_fingerprint=snapshot.catalog_fingerprint,
        catalog_provenance=snapshot.provenance,
        preview_host=host,
        host_fingerprint=host.fingerprint,
        policy_fingerprint=policy.policy_fingerprint,
        objective=CATALOG_FASTEST_OBJECTIVE_V1,
        purpose=ChainPurpose.SELECTION,
        candidate_ids=live_ids,
        algorithmically_selectable=ordered_ranked,
        statically_blocked=ordered_blocked,
        _factory_token=_RANKING_FACTORY_TOKEN,
    )
    return _RANKING_ISSUANCE.issue(receipt)


__all__ = [
    "CATALOG_FASTEST_OBJECTIVE_V1",
    "CatalogFastestCandidate",
    "CatalogFastestRankingReceipt",
    "CatalogRankingErrorCode",
    "CatalogRankingValidationError",
    "CatalogSelectedForComparison",
    "CatalogStaticAlgorithmBlockers",
    "CatalogStaticBlockedCandidate",
    "ExactRational",
    "is_catalog_fastest_ranking_receipt_issued",
    "rank_catalog_fastest",
]
