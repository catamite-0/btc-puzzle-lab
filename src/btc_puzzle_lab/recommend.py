"""Inventory-blind engine selection shared by planning and ``auto``.

This module maps the pure autopilot assessments to the small ``EngineChoice``
value consumed by the legacy runner. It does not inspect installed binaries,
compilers, CUDA, or the network; those are preparation concerns handled after
an algorithm family has been selected.
"""

from __future__ import annotations

from dataclasses import dataclass

from btc_puzzle_lab.autopilot.catalog_view import target_from_puzzle
from btc_puzzle_lab.autopilot.facts import EngineName, HostCapabilities, ResourceClass
from btc_puzzle_lab.autopilot.planning import (
    AlgorithmAssessment,
    Blocker,
    BlockerCode,
    PlanningPolicy,
    ProvisioningPolicy,
    assess_target_algorithms,
    select_algorithm_for_comparison,
)
from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.strategy import SAFE_DP
from btc_puzzle_lab.strategy import ResourceClass as LegacyResourceClass

_ENGINE_NAMES = frozenset(item.value for item in EngineName)
_PIN_PREFERENCE_BLOCKERS = frozenset({BlockerCode.BUILTIN_RANGE_PREFERRED})


@dataclass(frozen=True)
class EngineChoice:
    """One algorithm-family decision, detached from toolchain preparation."""

    engine: str
    resource: LegacyResourceClass
    reason: str
    provisioning: ProvisioningPolicy
    device_id: str | None = None
    dp: int | None = None
    blocked: str | None = None
    remedy: str | None = None

    @property
    def ok(self) -> bool:
        return self.blocked is None

    @property
    def needs_install(self) -> bool:
        return self.provisioning is not ProvisioningPolicy.BUILT_IN

    @property
    def manual_provisioning(self) -> bool:
        return self.provisioning is ProvisioningPolicy.MANUAL_REQUIRED

    def format(self) -> str:
        if not self.ok:
            lines = [f"blocked: {self.blocked}"]
            if self.remedy:
                lines.append(f"  remedy: {self.remedy}")
            return "\n".join(lines)
        bits = [f"engine={self.engine}", f"resource={self.resource}"]
        if self.device_id is not None:
            bits.append(f"device={self.device_id}")
        if self.dp is not None:
            bits.append(f"dp={self.dp}")
        if self.provisioning is ProvisioningPolicy.BUILT_IN:
            bits.append("build=not needed")
        elif self.provisioning is ProvisioningPolicy.MANUAL_REQUIRED:
            bits.append("provisioning=manual")
        else:
            bits.append("build=required")
        return f"{' '.join(bits)} — {self.reason}"


def _device_id(assessment: AlgorithmAssessment) -> str | None:
    return getattr(assessment.recipe, "device_id", None)


def _choice_from_assessment(
    assessment: AlgorithmAssessment,
    *,
    reason: str,
) -> EngineChoice:
    engine = assessment.engine.value
    return EngineChoice(
        engine=engine,
        resource=assessment.resource.value,
        reason=reason,
        provisioning=assessment.provisioning,
        device_id=_device_id(assessment),
        dp=SAFE_DP if engine in {"kangaroo", "rckangaroo"} else None,
    )


def _blocked_choice(
    assessment: AlgorithmAssessment,
    blockers: tuple[Blocker, ...],
    *,
    reason: str,
) -> EngineChoice:
    unique = tuple(dict.fromkeys((item.code.value, item.detail, item.remedy) for item in blockers))
    return EngineChoice(
        engine=assessment.engine.value,
        resource=assessment.resource.value,
        reason=reason,
        provisioning=assessment.provisioning,
        device_id=_device_id(assessment),
        dp=(SAFE_DP if assessment.engine in {EngineName.KANGAROO, EngineName.RCKANGAROO} else None),
        blocked=(
            "; ".join(f"{code}: {detail}" for code, detail, _remedy in unique)
            or f"{assessment.engine.value} has no complete recipe for this target and host"
        ),
        remedy=(
            "; ".join(dict.fromkeys(remedy for _code, _detail, remedy in unique))
            or "choose a compatible engine"
        ),
    )


def _no_choice(assessments: tuple[AlgorithmAssessment, ...]) -> EngineChoice:
    remedies = tuple(
        dict.fromkeys(
            blocker.remedy for assessment in assessments for blocker in assessment.blockers
        )
    )
    return EngineChoice(
        engine="none",
        resource="cpu",
        reason="shared planner could not select an algorithm family",
        provisioning=ProvisioningPolicy.BUILT_IN,
        blocked="no compatible algorithm for this target and exact host",
        remedy="; ".join(remedies) or "inspect the planner assessments",
    )


def _assess(
    puzzle: Puzzle,
    capabilities: HostCapabilities,
    *,
    policy: PlanningPolicy,
) -> tuple[AlgorithmAssessment, ...]:
    if type(capabilities) is not HostCapabilities:
        raise TypeError("capabilities must be an exact HostCapabilities value")
    return assess_target_algorithms(
        target_from_puzzle(puzzle),
        capabilities,
        policy=policy,
    )


def recommend_engine(
    puzzle: Puzzle,
    capabilities: HostCapabilities,
    *,
    cpu_only: bool = False,
) -> EngineChoice:
    """Select the fastest viable algorithm family from exact host facts."""

    assessments = _assess(puzzle, capabilities, policy=PlanningPolicy())
    candidates = (
        tuple(item for item in assessments if item.resource is ResourceClass.CPU)
        if cpu_only
        else assessments
    )
    selected = select_algorithm_for_comparison(candidates)
    if selected is None:
        return _no_choice(candidates)
    reason = "shared planner selected the fastest viable algorithm family"
    if cpu_only:
        reason += "; restricted to CPU by --allow-cpu-fallback"
    return _choice_from_assessment(selected, reason=reason)


def recommend_pinned_engine(
    puzzle: Puzzle,
    engine: str,
    *,
    capabilities: HostCapabilities,
    pin_source: str,
) -> EngineChoice:
    """Validate an explicit engine pin against the same planner assessments."""

    if type(engine) is not str or engine not in _ENGINE_NAMES:
        raise ValueError("pinned engine must be a planner EngineName")
    if (
        type(pin_source) is not str
        or not pin_source
        or pin_source != pin_source.strip()
        or any(ord(character) < 32 for character in pin_source)
    ):
        raise ValueError("pin_source must be non-empty trimmed text")

    assessments = _assess(
        puzzle,
        capabilities,
        policy=PlanningPolicy(
            allow_address_fallback_for_pubkey=True,
            allow_manual_provisioning=True,
        ),
    )
    selected_engine = EngineName(engine)
    selected = next(item for item in assessments if item.engine is selected_engine)
    hard_blockers = tuple(
        blocker for blocker in selected.blockers if blocker.code not in _PIN_PREFERENCE_BLOCKERS
    )
    reason = f"pinned by {pin_source}"
    if hard_blockers or selected.recipe is None or selected.estimate is None:
        return _blocked_choice(selected, hard_blockers, reason=reason)
    return _choice_from_assessment(selected, reason=reason)


__all__ = [
    "EngineChoice",
    "recommend_engine",
    "recommend_pinned_engine",
]
