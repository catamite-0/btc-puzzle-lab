"""Pure algorithm selection; executable parameters belong to preparation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import InitVar, dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fractions import Fraction
from typing import TypeAlias

from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogTargetBinding,
    is_catalog_target_binding_issued,
)
from btc_puzzle_lab.autopilot.chain import (
    ChainAdmissionReceipt,
    ChainEvidence,
    PracticeLookupBypass,
    is_chain_admission_receipt_issued,
    is_practice_lookup_bypass_issued,
)
from btc_puzzle_lab.autopilot.facts import (
    ChainPurpose,
    ChainSnapshot,
    ChainState,
    DomainValidationError,
    EngineName,
    GpuDevice,
    HostCapabilities,
    KeyRange,
    PuzzleTarget,
    ResourceClass,
    TargetMode,
)
from btc_puzzle_lab.autopilot.rck_memory import rck_base_allocation_bytes


class PlanningValidationError(DomainValidationError):
    pass


def _int(value: object, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PlanningValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _aware(value: object, name: str) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise PlanningValidationError(f"{name} must be a timezone-aware datetime")


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DERIVED_FINGERPRINTS = frozenset(("policy_fingerprint", "decision_fingerprint"))


def _safe_id(value: object, name: str) -> None:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise PlanningValidationError(f"{name} must be a lower-case safe identifier")


def _canonical(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _canonical(getattr(value, item.name))
            for item in fields(value)
            if item.name not in _DERIVED_FINGERPRINTS
        }
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Fraction):
        return [value.numerator, value.denominator]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise PlanningValidationError(f"cannot fingerprint {type(value).__name__}")


def _digest(value: object) -> str:
    raw = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


class ProvisioningPolicy(StrEnum):
    BUILT_IN = "built_in"
    AUTO_BUILD = "auto_build"
    MANUAL_REQUIRED = "manual_required"


class RestartSemantics(StrEnum):
    FREE = "free"
    WASTEFUL = "wasteful"
    DESTRUCTIVE = "destructive"


class EstimateSource(StrEnum):
    BASELINE = "baseline"


class BlockerCode(StrEnum):
    CHAIN_EVIDENCE_REQUIRED = "CHAIN_EVIDENCE_REQUIRED"
    PRACTICE_EVIDENCE_REQUIRED = "PRACTICE_EVIDENCE_REQUIRED"
    CHAIN_TARGET_MISMATCH = "CHAIN_TARGET_MISMATCH"
    CHAIN_PURPOSE_MISMATCH = "CHAIN_PURPOSE_MISMATCH"
    CHAIN_EVIDENCE_STALE = "CHAIN_EVIDENCE_STALE"
    PRIZE_UNKNOWN = "PRIZE_UNKNOWN"
    PRIZE_SWEPT = "PRIZE_SWEPT"
    PRIZE_UNCONFIRMED = "PRIZE_UNCONFIRMED"
    TARGET_SHAPE_UNSUPPORTED = "TARGET_SHAPE_UNSUPPORTED"
    PUBLIC_KEY_REQUIRED = "PUBLIC_KEY_REQUIRED"
    RANGE_UNSUPPORTED = "RANGE_UNSUPPORTED"
    BUILTIN_RANGE_PREFERRED = "BUILTIN_RANGE_PREFERRED"
    ADDRESS_FALLBACK_DISABLED = "ADDRESS_FALLBACK_DISABLED"
    MANUAL_PROVISIONING_DISABLED = "MANUAL_PROVISIONING_DISABLED"
    CPU_CAPACITY_UNAVAILABLE = "CPU_CAPACITY_UNAVAILABLE"
    GPU_MISSING = "GPU_MISSING"
    GPU_CAPABILITY_UNKNOWN = "GPU_CAPABILITY_UNKNOWN"
    GPU_CAPABILITY_UNSUPPORTED = "GPU_CAPABILITY_UNSUPPORTED"
    HOST_MEMORY_INSUFFICIENT = "HOST_MEMORY_INSUFFICIENT"
    GPU_MEMORY_INSUFFICIENT = "GPU_MEMORY_INSUFFICIENT"
    NO_COMPATIBLE_ALGORITHM = "NO_COMPATIBLE_ALGORITHM"


@dataclass(frozen=True, slots=True, kw_only=True)
class Blocker:
    code: BlockerCode
    detail: str
    remedy: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanningPolicy:
    objective: str = "fastest"
    planning_horizon_seconds: int = 86_400
    memory_safety_fraction: Fraction = Fraction(3, 4)
    cpu_reserved_cores: int = 1
    allow_address_fallback_for_pubkey: bool = False
    allow_manual_provisioning: bool = False
    policy_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.objective) is not str or self.objective != "fastest":
            raise PlanningValidationError("only the fastest objective is implemented")
        _int(self.planning_horizon_seconds, "planning_horizon_seconds", 1)
        _int(self.cpu_reserved_cores, "cpu_reserved_cores")
        if type(self.allow_address_fallback_for_pubkey) is not bool:
            raise PlanningValidationError("allow_address_fallback_for_pubkey must be boolean")
        if type(self.allow_manual_provisioning) is not bool:
            raise PlanningValidationError("allow_manual_provisioning must be boolean")
        if type(self.memory_safety_fraction) is not Fraction or not (
            0 < self.memory_safety_fraction <= 1
        ):
            raise PlanningValidationError("memory_safety_fraction must be a Fraction in (0, 1]")
        object.__setattr__(self, "policy_fingerprint", _digest(self))


def _ranges(value: object) -> None:
    if type(value) is not tuple or any(type(item) is not KeyRange for item in value):
        raise PlanningValidationError("remaining_ranges must contain KeyRange values")
    if any(left.end >= right.start for left, right in zip(value, value[1:], strict=False)):
        raise PlanningValidationError("remaining_ranges must be ordered and disjoint")


@dataclass(frozen=True, slots=True, kw_only=True)
class SequentialRecipeV1:
    remaining_ranges: tuple[KeyRange, ...]
    adapter_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        _ranges(self.remaining_ranges)


@dataclass(frozen=True, slots=True, kw_only=True)
class KeyhuntRecipeV1:
    remaining_ranges: tuple[KeyRange, ...]
    adapter_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        _ranges(self.remaining_ranges)


@dataclass(frozen=True, slots=True, kw_only=True)
class KangarooRecipeV1:
    engine: EngineName
    range_start: int
    range_end: int
    range_exponent: int
    public_key_hex: str
    device_id: str | None
    distinguished_point_bits_min: int | None
    distinguished_point_bits_max: int | None
    adapter_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        if type(self.engine) is not EngineName or self.engine not in (
            EngineName.KANGAROO,
            EngineName.RCKANGAROO,
        ):
            raise PlanningValidationError("kangaroo recipe requires a kangaroo engine")
        _int(self.range_start, "range_start", 1)
        _int(self.range_end, "range_end", self.range_start)
        _int(self.range_exponent, "range_exponent", 1)
        if type(self.public_key_hex) is not str or not re.fullmatch(
            r"0[23][0-9a-f]{64}", self.public_key_hex
        ):
            raise PlanningValidationError("public_key_hex must be compressed SEC hex")
        if self.engine is EngineName.KANGAROO:
            if self.device_id is not None or any(
                item is not None
                for item in (
                    self.distinguished_point_bits_min,
                    self.distinguished_point_bits_max,
                )
            ):
                raise PlanningValidationError("CPU kangaroo recipe contains GPU-only bounds")
        else:
            if (
                type(self.device_id) is not str
                or not self.device_id
                or self.device_id != self.device_id.strip()
            ):
                raise PlanningValidationError("RCK recipe requires only a selected device")
            if self.range_exponent < 32 or self.range_exponent > 170:
                raise PlanningValidationError("RCK range exponent must be in [32, 170]")
            if self.range_end - self.range_start + 1 != 1 << self.range_exponent:
                raise PlanningValidationError("RCK range must contain exactly a power of two keys")
            if self.distinguished_point_bits_min != 14 or self.distinguished_point_bits_max != 32:
                raise PlanningValidationError("RCK DP bounds must be [14, 32]")


@dataclass(frozen=True, slots=True, kw_only=True)
class BitCrackRecipeV1:
    remaining_ranges: tuple[KeyRange, ...]
    device_id: str
    adapter_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        _ranges(self.remaining_ranges)
        if (
            type(self.device_id) is not str
            or not self.device_id
            or self.device_id != self.device_id.strip()
            or len(self.device_id) > 128
        ):
            raise PlanningValidationError("device_id must be bounded non-empty trimmed text")


AlgorithmRecipe: TypeAlias = (
    SequentialRecipeV1 | KeyhuntRecipeV1 | KangarooRecipeV1 | BitCrackRecipeV1
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExactEstimate:
    model_version: str
    source: EstimateSource
    source_fingerprint: str
    work_unit: str
    full_work: int | None
    horizon_work_limit: int
    full_solution_expected_work: Fraction
    horizon_expected_occupied_work: Fraction | None
    horizon_hit_probability: Fraction | None
    assumed_rate_per_second: int
    confidence: Fraction
    full_solution_eta_seconds: Fraction = field(init=False)
    horizon_expected_occupied_seconds: Fraction | None = field(init=False)

    def __post_init__(self) -> None:
        _safe_id(self.model_version, "model_version")
        if type(self.source) is not EstimateSource:
            raise PlanningValidationError("source must be EstimateSource")
        if not _SHA256.fullmatch(self.source_fingerprint):
            raise PlanningValidationError("source_fingerprint must be a SHA-256 digest")
        _safe_id(self.work_unit, "work_unit")
        if self.full_work is not None:
            _int(self.full_work, "full_work", 1)
        _int(self.horizon_work_limit, "horizon_work_limit", 1)
        if (
            not isinstance(self.full_solution_expected_work, Fraction)
            or self.full_solution_expected_work <= 0
        ):
            raise PlanningValidationError("full_solution_expected_work must be positive")
        occupied = self.horizon_expected_occupied_work
        if occupied is not None and (not isinstance(occupied, Fraction) or occupied <= 0):
            raise PlanningValidationError("horizon_expected_occupied_work must be positive")
        probability = self.horizon_hit_probability
        if probability is not None and (
            not isinstance(probability, Fraction) or not 0 <= probability <= 1
        ):
            raise PlanningValidationError("horizon_hit_probability must be a Fraction in [0, 1]")
        _int(self.assumed_rate_per_second, "assumed_rate_per_second", 1)
        if not isinstance(self.confidence, Fraction) or not 0 < self.confidence <= 1:
            raise PlanningValidationError("confidence must be a Fraction in (0, 1]")
        object.__setattr__(
            self,
            "full_solution_eta_seconds",
            self.full_solution_expected_work / self.assumed_rate_per_second,
        )
        object.__setattr__(
            self,
            "horizon_expected_occupied_seconds",
            occupied / self.assumed_rate_per_second if occupied is not None else None,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AlgorithmAssessment:
    engine: EngineName
    resource: ResourceClass
    provisioning: ProvisioningPolicy
    restart: RestartSemantics
    exact_checkpoint: bool
    recipe: AlgorithmRecipe | None
    estimate: ExactEstimate | None
    required_host_memory_floor_bytes: int | None
    required_device_memory_floor_bytes: int | None
    explanation: str
    blockers: tuple[Blocker, ...]

    @property
    def viable(self) -> bool:
        return not self.blockers and self.recipe is not None and self.estimate is not None


_DECISION_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectionDecision:
    target: PuzzleTarget
    catalog_fingerprint: str
    chain_snapshot: ChainSnapshot | None
    chain_evidence_fingerprint: str
    host: HostCapabilities
    policy: PlanningPolicy
    evaluated_at: datetime
    selected: AlgorithmAssessment
    decision_fingerprint: str = field(init=False)
    _factory_token: InitVar[object | None] = None

    def __init_subclass__(cls, **kwargs: object) -> None:
        del cls, kwargs
        raise TypeError("SelectionDecision is final and cannot be subclassed")

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _DECISION_FACTORY_TOKEN:
            raise PlanningValidationError("selection decisions must come from plan_target")
        if not _SHA256.fullmatch(self.catalog_fingerprint):
            raise PlanningValidationError("catalog fingerprint is malformed")
        if not self.selected.viable:
            raise PlanningValidationError("selected algorithm must be viable")
        if (
            self.selected.provisioning is ProvisioningPolicy.MANUAL_REQUIRED
            and not self.policy.allow_manual_provisioning
        ):
            raise PlanningValidationError("manual provisioning requires explicit policy opt-in")
        _aware(self.evaluated_at, "evaluated_at")
        recipe = self.selected.recipe
        if isinstance(recipe, KangarooRecipeV1):
            if (
                recipe.engine is not self.selected.engine
                or recipe.range_start != self.target.key_range.start
                or recipe.range_end != self.target.key_range.end
                or recipe.public_key_hex != self.target.public_key_hex
            ):
                raise PlanningValidationError("kangaroo recipe does not match the target")
        else:
            if recipe.remaining_ranges != (self.target.key_range,) or any(
                not self.target.key_range.contains_range(item) for item in recipe.remaining_ranges
            ):
                raise PlanningValidationError("deterministic recipe does not match the target")
        if self.selected.resource is ResourceClass.GPU and self.host.gpu(recipe.device_id) is None:
            raise PlanningValidationError("GPU recipe does not match a detected device")
        payload = {
            "contract_version": 2,
            "target": self.target,
            "catalog": self.catalog_fingerprint,
            "chain_snapshot": (
                self.chain_snapshot.evidence_fingerprint if self.chain_snapshot else None
            ),
            "chain_evidence": self.chain_evidence_fingerprint,
            "host": self.host.fingerprint,
            "policy": self.policy.policy_fingerprint,
            "objective": self.policy.objective,
            "evaluated_at": self.evaluated_at,
            "algorithm": self.selected,
        }
        object.__setattr__(self, "decision_fingerprint", _digest(payload))


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanningResult:
    assessments: tuple[AlgorithmAssessment, ...]
    target_blockers: tuple[Blocker, ...]
    decision: SelectionDecision | None


_BUILTIN_LIMIT = 2_000_000
_BASELINE_CONFIDENCE = Fraction(1, 10)
_RCK_CAPABILITIES = frozenset(((8, 9), (12, 0)))
_BASELINES = {
    EngineName.SEQUENTIAL: 25_000,
    EngineName.KEYHUNT: 5_000_000,
    EngineName.KANGAROO: 1_000_000,
    EngineName.RCKANGAROO: 500_000_000,
    EngineName.BITCRACK: 250_000_000,
}


def _block(code: BlockerCode, detail: str, remedy: str) -> Blocker:
    return Blocker(code=code, detail=detail, remedy=remedy)


def _one(code: BlockerCode, detail: str, remedy: str) -> tuple[Blocker, ...]:
    return (_block(code, detail, remedy),)


def _add(blockers: list[Blocker], code: BlockerCode, detail: str, remedy: str) -> None:
    blockers.append(_block(code, detail, remedy))


def _budget(total: int, policy: PlanningPolicy) -> int:
    value = policy.memory_safety_fraction
    return total * value.numerator // value.denominator


def _width(host: HostCapabilities, policy: PlanningPolicy, limit: int) -> int:
    return min(host.cpu_count - policy.cpu_reserved_cores, limit)


def _baseline(
    engine: EngineName,
    width: int,
) -> tuple[int, str]:
    rate = _BASELINES[engine] * width
    fingerprint = _digest(
        {
            "schema": "conservative-selection-baseline-v1",
            "engine": engine.value,
            "rate": rate,
        }
    )
    return rate, fingerprint


def _deterministic(
    keys: int,
    horizon: int,
    model: str,
    baseline: tuple[int, str],
) -> ExactEstimate:
    rate, source_fingerprint = baseline
    planned = min(keys, rate * horizon)
    return ExactEstimate(
        model_version=model,
        source=EstimateSource.BASELINE,
        source_fingerprint=source_fingerprint,
        work_unit="private_key_test",
        full_work=keys,
        horizon_work_limit=planned,
        full_solution_expected_work=Fraction(keys + 1, 2),
        horizon_expected_occupied_work=Fraction(planned * (2 * keys - planned + 1), 2 * keys),
        horizon_hit_probability=Fraction(planned, keys),
        assumed_rate_per_second=rate,
        confidence=_BASELINE_CONFIDENCE,
    )


def _kangaroo_work(
    size: int,
    horizon: int,
    model: str,
    baseline: tuple[int, str],
) -> ExactEstimate:
    rate, source_fingerprint = baseline
    root = math.isqrt(size)
    root += root * root != size
    return ExactEstimate(
        model_version=model,
        source=EstimateSource.BASELINE,
        source_fingerprint=source_fingerprint,
        work_unit="group_operation",
        full_work=None,
        horizon_work_limit=rate * horizon,
        full_solution_expected_work=Fraction(2 * root),
        horizon_expected_occupied_work=None,
        horizon_hit_probability=None,
        assumed_rate_per_second=rate,
        confidence=_BASELINE_CONFIDENCE,
    )


def _bitcrack_gpu(host: HostCapabilities) -> tuple[GpuDevice | None, tuple[Blocker, ...]]:
    if not host.gpus:
        return None, _one(BlockerCode.GPU_MISSING, "no physical GPU detected", "use a GPU host")
    supported = tuple(
        item
        for item in host.gpus
        if item.compute_capability is not None and item.compute_capability >= (5, 0)
    )
    if supported:
        return min(supported, key=lambda item: (-item.memory_bytes, item.device_id)), ()
    code = (
        BlockerCode.GPU_CAPABILITY_UNKNOWN
        if any(item.compute_capability is None for item in host.gpus)
        else BlockerCode.GPU_CAPABILITY_UNSUPPORTED
    )
    return None, _one(code, "no physical GPU supports BitCrack", "select a supported GPU")


def _rck_gpu(
    host: HostCapabilities,
    policy: PlanningPolicy,
) -> tuple[GpuDevice | None, int | None, int | None, tuple[Blocker, ...]]:
    if not host.gpus:
        return (
            None,
            None,
            None,
            _one(BlockerCode.GPU_MISSING, "no physical GPU detected", "use a GPU host"),
        )
    supported_cc = tuple(item for item in host.gpus if item.compute_capability in _RCK_CAPABILITIES)
    topology: list[tuple[GpuDevice, int, int]] = []
    for device in supported_cc:
        if device.multiprocessor_count is None:
            continue
        try:
            host_floor, device_floor = rck_base_allocation_bytes(
                sm_count=device.multiprocessor_count,
                compute_capability=device.compute_capability,
            )
        except ValueError:
            continue
        topology.append((device, host_floor, device_floor))
    if not topology:
        if supported_cc:
            return (
                None,
                None,
                None,
                _one(
                    BlockerCode.GPU_CAPABILITY_UNKNOWN,
                    "RCK v4 requires a known valid SM count",
                    "rediscover physical GPU topology",
                ),
            )
        code = (
            BlockerCode.GPU_CAPABILITY_UNKNOWN
            if any(item.compute_capability is None for item in host.gpus)
            else BlockerCode.GPU_CAPABILITY_UNSUPPORTED
        )
        return (
            None,
            None,
            None,
            _one(code, "no physical GPU supports RCK v4", "select sm_89 or sm_120"),
        )
    host_budget = _budget(host.memory_bytes, policy)

    def _memory_blocks(candidate: tuple[GpuDevice, int, int]) -> tuple[Blocker, ...]:
        device, host_floor, device_floor = candidate
        blockers: list[Blocker] = []
        if host_floor > host_budget:
            _add(
                blockers,
                BlockerCode.HOST_MEMORY_INSUFFICIENT,
                f"RCK v4 source-derived host startup allocation floor {host_floor} "
                "exceeds safe budget",
                "select a host with more physical memory",
            )
        if device_floor > _budget(device.memory_bytes, policy):
            _add(
                blockers,
                BlockerCode.GPU_MEMORY_INSUFFICIENT,
                f"RCK v4 source-derived GPU allocation floor {device_floor} exceeds safe budget",
                "select a GPU with more physical memory",
            )
        return tuple(blockers)

    ranked = sorted(
        topology,
        key=lambda item: (-item[0].multiprocessor_count, -item[0].memory_bytes, item[0].device_id),
    )
    for device, host_floor, device_floor in ranked:
        if not _memory_blocks((device, host_floor, device_floor)):
            return device, host_floor, device_floor, ()

    candidate = min(
        ranked,
        key=lambda item: (
            len(_memory_blocks(item)),
            -item[0].multiprocessor_count,
            -item[0].memory_bytes,
            item[0].device_id,
        ),
    )
    device, host_floor, device_floor = candidate
    return None, host_floor, device_floor, _memory_blocks(candidate)


def _shape(target: PuzzleTarget) -> Blocker | None:
    if target.address.startswith("1"):
        return None
    return _block(
        BlockerCode.TARGET_SHAPE_UNSUPPORTED,
        "adapters currently support compressed P2PKH targets only",
        "add a verified adapter for this address type",
    )


def _pubkey(target: PuzzleTarget) -> Blocker | None:
    if target.public_key_hex and len(target.public_key_hex) == 66:
        return None
    return _block(
        BlockerCode.PUBLIC_KEY_REQUIRED,
        "a matching compressed public key is required",
        "choose a target with a verified compressed public key",
    )


def _range_exponent(target: PuzzleTarget) -> int:
    return max(1, (target.range_size - 1).bit_length())


def _kangaroo_supports_range(target: PuzzleTarget, *, rck: bool) -> bool:
    """Return range support without consulting host or engine inventory."""

    exponent = _range_exponent(target)
    if not rck:
        return exponent >= 31
    power_of_two = target.range_size & (target.range_size - 1) == 0
    return power_of_two and 32 <= exponent <= 170


def _address_fallback_is_dominated(target: PuzzleTarget) -> bool:
    """Whether any public-key family theoretically supports this exact range."""

    return target.has_public_key and any(
        _kangaroo_supports_range(target, rck=rck) for rck in (False, True)
    )


def _assessment(
    engine: EngineName,
    resource: ResourceClass,
    provisioning: ProvisioningPolicy,
    restart: RestartSemantics,
    recipe: AlgorithmRecipe | None,
    estimate: ExactEstimate | None,
    blockers: list[Blocker],
    *,
    host_floor: int | None = None,
    device_floor: int | None = None,
    checkpoint: bool = False,
) -> AlgorithmAssessment:
    explanation = (
        "source-derived host startup and GPU allocation floors only; CUDA context, final DP, "
        "and horizon dynamic memory remain Preparation gates; throughput baseline was not "
        "measured on this host"
        if engine is EngineName.RCKANGAROO
        else "versioned conservative baseline; not measured on this host"
    )
    return AlgorithmAssessment(
        engine=engine,
        resource=resource,
        provisioning=provisioning,
        restart=restart,
        exact_checkpoint=checkpoint,
        recipe=recipe,
        estimate=estimate,
        required_host_memory_floor_bytes=host_floor,
        required_device_memory_floor_bytes=device_floor,
        explanation=explanation,
        blockers=tuple(blockers),
    )


def _sequential(
    target: PuzzleTarget,
    host: HostCapabilities,
    policy: PlanningPolicy,
) -> AlgorithmAssessment:
    ranges = (target.key_range,)
    blockers = [item for item in (_shape(target),) if item]
    if target.range_size > _BUILTIN_LIMIT:
        _add(
            blockers,
            BlockerCode.RANGE_UNSUPPORTED,
            f"range size {target.range_size} exceeds built-in limit {_BUILTIN_LIMIT}",
            "use an external compatible adapter",
        )
    workers = _width(host, policy, 4)
    if workers <= 0:
        _add(
            blockers,
            BlockerCode.CPU_CAPACITY_UNAVAILABLE,
            "reserved CPU cores leave no capacity for sequential search",
            "reduce reserved cores or use another resource",
        )
    keys = sum(item.size for item in ranges)
    baseline = _baseline(EngineName.SEQUENTIAL, workers) if workers > 0 else None
    recipe = SequentialRecipeV1(remaining_ranges=ranges) if workers > 0 else None
    estimate = (
        _deterministic(keys, policy.planning_horizon_seconds, "sequential-selection-v1", baseline)
        if keys and baseline
        else None
    )
    return _assessment(
        EngineName.SEQUENTIAL,
        ResourceClass.CPU,
        ProvisioningPolicy.BUILT_IN,
        RestartSemantics.FREE,
        recipe,
        estimate,
        blockers,
        checkpoint=True,
    )


def _keyhunt(
    target: PuzzleTarget,
    host: HostCapabilities,
    policy: PlanningPolicy,
    *,
    address_fallback_is_dominated: bool,
) -> AlgorithmAssessment:
    ranges = (target.key_range,)
    blockers = [item for item in (_shape(target),) if item]
    if target.range_size <= _BUILTIN_LIMIT:
        _add(
            blockers,
            BlockerCode.BUILTIN_RANGE_PREFERRED,
            "range fits the built-in scanner",
            "use the no-toolchain built-in selection",
        )
    if address_fallback_is_dominated and not policy.allow_address_fallback_for_pubkey:
        _add(
            blockers,
            BlockerCode.ADDRESS_FALLBACK_DISABLED,
            "pubkey kangaroo dominates address brute force",
            "explicitly enable dominated address fallback",
        )
    threads = _width(host, policy, 8)
    if threads <= 0:
        _add(
            blockers,
            BlockerCode.CPU_CAPACITY_UNAVAILABLE,
            "reserved CPU cores leave no capacity for keyhunt",
            "reduce reserved cores or use another resource",
        )
    keys = sum(item.size for item in ranges)
    baseline = _baseline(EngineName.KEYHUNT, threads) if threads > 0 else None
    recipe = KeyhuntRecipeV1(remaining_ranges=ranges) if threads > 0 else None
    estimate = (
        _deterministic(keys, policy.planning_horizon_seconds, "keyhunt-selection-v1", baseline)
        if keys and baseline
        else None
    )
    return _assessment(
        EngineName.KEYHUNT,
        ResourceClass.CPU,
        ProvisioningPolicy.AUTO_BUILD,
        RestartSemantics.WASTEFUL,
        recipe,
        estimate,
        blockers,
    )


def _kangaroo(
    target: PuzzleTarget,
    host: HostCapabilities,
    policy: PlanningPolicy,
    *,
    rck: bool,
) -> AlgorithmAssessment:
    engine = EngineName.RCKANGAROO if rck else EngineName.KANGAROO
    blockers = [item for item in (_pubkey(target),) if item]
    exponent = _range_exponent(target)
    range_supported = _kangaroo_supports_range(target, rck=rck)
    device = None
    host_floor = None
    device_floor = None
    if rck:
        if not policy.allow_manual_provisioning:
            _add(
                blockers,
                BlockerCode.MANUAL_PROVISIONING_DISABLED,
                "RCK v4 requires manual provisioning and is disabled by automatic policy",
                "explicitly allow manual provisioning for this selection",
            )
        if not range_supported:
            _add(
                blockers,
                BlockerCode.RANGE_UNSUPPORTED,
                "RCK v4 requires an exact power-of-two range with exponent 32..170",
                "use another compatible algorithm",
            )
        device, host_floor, device_floor, gpu_blocks = _rck_gpu(host, policy)
        blockers.extend(gpu_blocks)
        width = 1
    else:
        if not range_supported:
            _add(
                blockers,
                BlockerCode.RANGE_UNSUPPORTED,
                "CPU kangaroo requires a range exponent of at least 31",
                "use another compatible algorithm",
            )
        width = _width(host, policy, 8)
        if width <= 0:
            _add(
                blockers,
                BlockerCode.CPU_CAPACITY_UNAVAILABLE,
                "reserved CPU cores leave no capacity for CPU kangaroo",
                "reduce reserved cores or use another resource",
            )
    baseline = _baseline(engine, width) if width > 0 else None
    pubkey = target.public_key_hex
    if rck:
        recipe = (
            KangarooRecipeV1(
                engine=engine,
                range_start=target.key_range.start,
                range_end=target.key_range.end,
                range_exponent=exponent,
                public_key_hex=pubkey,
                device_id=device.device_id,
                distinguished_point_bits_min=14,
                distinguished_point_bits_max=32,
            )
            if pubkey and device and range_supported
            else None
        )
    else:
        recipe = (
            KangarooRecipeV1(
                engine=engine,
                range_start=target.key_range.start,
                range_end=target.key_range.end,
                range_exponent=exponent,
                public_key_hex=pubkey,
                device_id=None,
                distinguished_point_bits_min=None,
                distinguished_point_bits_max=None,
            )
            if pubkey and width > 0
            else None
        )
    estimate = (
        _kangaroo_work(
            target.range_size,
            policy.planning_horizon_seconds,
            f"{engine.value}-selection-v1",
            baseline,
        )
        if recipe and baseline
        else None
    )
    return _assessment(
        engine,
        ResourceClass.GPU if rck else ResourceClass.CPU,
        ProvisioningPolicy.MANUAL_REQUIRED if rck else ProvisioningPolicy.AUTO_BUILD,
        RestartSemantics.DESTRUCTIVE,
        recipe,
        estimate,
        blockers,
        host_floor=host_floor,
        device_floor=device_floor,
    )


def _bitcrack(
    target: PuzzleTarget,
    host: HostCapabilities,
    policy: PlanningPolicy,
    *,
    address_fallback_is_dominated: bool,
) -> AlgorithmAssessment:
    ranges = (target.key_range,)
    blockers = [item for item in (_shape(target),) if item]
    if target.range_size <= _BUILTIN_LIMIT:
        _add(
            blockers,
            BlockerCode.BUILTIN_RANGE_PREFERRED,
            "range fits the built-in scanner",
            "use the no-toolchain built-in selection",
        )
    if address_fallback_is_dominated and not policy.allow_address_fallback_for_pubkey:
        _add(
            blockers,
            BlockerCode.ADDRESS_FALLBACK_DISABLED,
            "pubkey kangaroo dominates address brute force",
            "explicitly enable dominated address fallback",
        )
    device, gpu_blocks = _bitcrack_gpu(host)
    blockers.extend(gpu_blocks)
    baseline = _baseline(EngineName.BITCRACK, 1)
    recipe = (
        BitCrackRecipeV1(remaining_ranges=ranges, device_id=device.device_id)
        if device and ranges
        else None
    )
    keys = sum(item.size for item in ranges)
    estimate = (
        _deterministic(keys, policy.planning_horizon_seconds, "bitcrack-selection-v1", baseline)
        if recipe and keys
        else None
    )
    return _assessment(
        EngineName.BITCRACK,
        ResourceClass.GPU,
        ProvisioningPolicy.AUTO_BUILD,
        RestartSemantics.WASTEFUL,
        recipe,
        estimate,
        blockers,
    )


def assess_target_algorithms(
    target: PuzzleTarget,
    host: HostCapabilities,
    *,
    policy: PlanningPolicy,
) -> tuple[AlgorithmAssessment, ...]:
    """Return every static algorithm assessment for one target and host.

    This is the sole assessment-generation path used by both single-target
    planning and complete-catalog comparison.  It is pure: it does not inspect
    chain state, engine inventory, files, environment variables, or the current
    machine.  ``host`` is therefore a descriptive preview fact; preparation
    must rediscover and admit the effective host before execution.
    """

    if type(target) is not PuzzleTarget or type(host) is not HostCapabilities:
        raise PlanningValidationError("target and host must be exact typed facts")
    if type(policy) is not PlanningPolicy:
        raise PlanningValidationError("policy must be PlanningPolicy")

    address_fallback_is_dominated = _address_fallback_is_dominated(target)
    return (
        _sequential(target, host, policy),
        _keyhunt(
            target,
            host,
            policy,
            address_fallback_is_dominated=address_fallback_is_dominated,
        ),
        _kangaroo(target, host, policy, rck=False),
        _kangaroo(target, host, policy, rck=True),
        _bitcrack(
            target,
            host,
            policy,
            address_fallback_is_dominated=address_fallback_is_dominated,
        ),
    )


def select_algorithm_for_comparison(
    assessments: tuple[AlgorithmAssessment, ...],
) -> AlgorithmAssessment | None:
    """Choose the deterministic fastest-baseline assessment, if one exists."""

    if type(assessments) is not tuple or any(
        type(assessment) is not AlgorithmAssessment for assessment in assessments
    ):
        raise PlanningValidationError(
            "assessments must be an exact tuple of AlgorithmAssessment values"
        )
    algorithmically_selectable = tuple(
        assessment for assessment in assessments if assessment.viable
    )
    if not algorithmically_selectable:
        return None
    return min(
        algorithmically_selectable,
        key=lambda assessment: (
            assessment.estimate.full_solution_eta_seconds,
            -assessment.estimate.confidence,
            assessment.engine.value,
        ),
    )


def algorithm_assessment_fingerprint(assessment: AlgorithmAssessment) -> str:
    """Fingerprint every field of one immutable descriptive assessment."""

    if type(assessment) is not AlgorithmAssessment:
        raise PlanningValidationError("assessment must be an AlgorithmAssessment")
    return _digest({"contract_version": 1, "assessment": assessment})


def _target_blocks(
    binding: CatalogTargetBinding, evidence: ChainEvidence, at: datetime
) -> tuple[Blocker, ...]:
    target = binding.target
    if target.mode is TargetMode.PRACTICE:
        if type(evidence) is not PracticeLookupBypass:
            return _one(
                BlockerCode.PRACTICE_EVIDENCE_REQUIRED,
                "practice selection requires a catalog-bound lookup bypass",
                "collect practice evidence through the catalog and chain adapter",
            )
        if evidence.target != target or evidence.fixture != binding.practice_fixture:
            return _one(
                BlockerCode.CHAIN_TARGET_MISMATCH,
                "practice evidence does not match the exact catalog binding",
                "collect evidence from this catalog binding",
            )
        if evidence.purpose is not ChainPurpose.SELECTION:
            return _one(
                BlockerCode.CHAIN_PURPOSE_MISMATCH,
                "selection requires selection-purpose practice evidence",
                "collect a selection-purpose bypass",
            )
        return ()
    if type(evidence) is not ChainAdmissionReceipt:
        return _one(
            BlockerCode.CHAIN_EVIDENCE_REQUIRED,
            "live selection requires an authorized chain receipt",
            "refresh through configured provider adapters",
        )
    snapshot = evidence.snapshot
    if (
        evidence.target != target
        or snapshot.target_id != target.puzzle_id
        or snapshot.address != target.address
    ):
        return _one(
            BlockerCode.CHAIN_TARGET_MISMATCH,
            "chain receipt belongs to another target",
            "refresh this exact target",
        )
    if snapshot.purpose is not ChainPurpose.SELECTION:
        return _one(
            BlockerCode.CHAIN_PURPOSE_MISMATCH,
            "selection requires selection-purpose evidence",
            "refresh a selection snapshot",
        )
    if not snapshot.is_fresh(at, purpose=ChainPurpose.SELECTION):
        return _one(
            BlockerCode.CHAIN_EVIDENCE_STALE,
            "provider evidence is stale or from the future",
            "refresh every provider",
        )
    if snapshot.state is ChainState.FUNDED_CONFIRMED:
        return ()
    code, detail, remedy = {
        ChainState.UNKNOWN: (
            BlockerCode.PRIZE_UNKNOWN,
            f"provider quorum cannot establish prize state: {snapshot.unknown_reason}",
            "restore provider quorum",
        ),
        ChainState.EMPTY: (
            BlockerCode.PRIZE_SWEPT,
            "fresh evidence agrees the target has no UTXOs",
            "choose another funded target",
        ),
        ChainState.FUNDED_UNCONFIRMED: (
            BlockerCode.PRIZE_UNCONFIRMED,
            "only unconfirmed value exists",
            "wait for confirmation and refresh",
        ),
    }[snapshot.state]
    return (_block(code, detail, remedy),)


def plan_target(
    binding: CatalogTargetBinding,
    chain_evidence: ChainEvidence,
    host: HostCapabilities,
    *,
    evaluated_at: datetime,
    policy: PlanningPolicy | None = None,
) -> PlanningResult:
    if type(binding) is not CatalogTargetBinding or type(host) is not HostCapabilities:
        raise PlanningValidationError("catalog binding and host must be typed facts")
    if not is_catalog_target_binding_issued(binding):
        raise PlanningValidationError(
            "catalog binding must be issued by an unchanged validated snapshot"
        )
    if type(chain_evidence) not in (ChainAdmissionReceipt, PracticeLookupBypass):
        raise PlanningValidationError("chain evidence must come from registered collection")
    if (
        type(chain_evidence) is ChainAdmissionReceipt
        and not is_chain_admission_receipt_issued(chain_evidence)
    ) or (
        type(chain_evidence) is PracticeLookupBypass
        and not is_practice_lookup_bypass_issued(chain_evidence)
    ):
        raise PlanningValidationError(
            "chain evidence must come unchanged from registered collection"
        )
    _aware(evaluated_at, "evaluated_at")
    if policy is None:
        chosen_policy = PlanningPolicy()
    elif type(policy) is PlanningPolicy:
        chosen_policy = policy
    else:
        raise PlanningValidationError("policy must be PlanningPolicy")
    target = binding.target
    chain_snapshot = (
        chain_evidence.snapshot if type(chain_evidence) is ChainAdmissionReceipt else None
    )
    evidence_fingerprint = chain_evidence.receipt_fingerprint
    if not _SHA256.fullmatch(evidence_fingerprint):
        raise PlanningValidationError("chain evidence is malformed")

    assessments = assess_target_algorithms(
        target,
        host,
        policy=chosen_policy,
    )
    target_blockers = list(_target_blocks(binding, chain_evidence, evaluated_at))
    selected = select_algorithm_for_comparison(assessments)
    if selected is None:
        target_blockers.append(
            _block(
                BlockerCode.NO_COMPATIBLE_ALGORITHM,
                "every adapter failed a hard gate",
                "inspect algorithm blockers",
            )
        )
    decision = None
    if not target_blockers:
        assert selected is not None  # established by the no-compatible blocker above
        decision = SelectionDecision(
            target=target,
            catalog_fingerprint=binding.catalog_fingerprint,
            chain_snapshot=chain_snapshot,
            chain_evidence_fingerprint=evidence_fingerprint,
            host=host,
            policy=chosen_policy,
            evaluated_at=evaluated_at,
            selected=selected,
            _factory_token=_DECISION_FACTORY_TOKEN,
        )
    return PlanningResult(
        assessments=tuple(assessments),
        target_blockers=tuple(target_blockers),
        decision=decision,
    )
