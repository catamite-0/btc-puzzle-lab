import json
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest

from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogTargetBinding,
    CatalogTargetError,
    snapshot_from_puzzles,
)
from btc_puzzle_lab.autopilot.chain import (
    ChainAdmissionReceipt,
    FixtureProvider,
    PracticeLookupBypass,
    ProviderRegistry,
    ProviderResource,
    RawHttpResponse,
    collect_chain_evidence,
)
from btc_puzzle_lab.autopilot.facts import (
    ChainPurpose,
    ChainSnapshot,
    ChainUtxo,
    GpuDevice,
    HostCapabilities,
    KeyRange,
    PuzzleTarget,
)
from btc_puzzle_lab.autopilot.planning import (
    BitCrackRecipeV1,
    BlockerCode,
    EngineName,
    EstimateSource,
    KangarooRecipeV1,
    KeyhuntRecipeV1,
    PlanningPolicy,
    PlanningValidationError,
    ProvisioningPolicy,
    SelectionDecision,
    SequentialRecipeV1,
    plan_target,
)
from btc_puzzle_lab.catalog import Puzzle, get_puzzle, load_packaged_full_puzzles
from btc_puzzle_lab.crypto import address_hash160

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
ADDRESS = "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
ADDRESS_SCRIPT = "76a914751e76e8199196d454941c45d1b3a323f1433bd688ac"
PUBKEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
GIB = 1024**3
FULL_CATALOG = snapshot_from_puzzles(tuple(load_packaged_full_puzzles()))


@dataclass(frozen=True)
class _AdapterReceipt:
    snapshot: ChainSnapshot
    receipt_fingerprint: str


def _puzzle(
    *,
    puzzle_id: int = 71,
    end: int = 1 << 40,
    public_key: bool = False,
    bits_label: int = 140,
    address: str = ADDRESS,
) -> Puzzle:
    return Puzzle(
        id=puzzle_id,
        bits=bits_label,
        address=address,
        range_start=1,
        range_end=end,
        pubkey_compressed_hex=PUBKEY if public_key else "",
        practice_solution=None,
        status="unsolved",
        engine_default="auto",
        notes="planning test target",
    )


def _binding(**changes) -> CatalogTargetBinding:
    puzzle = _puzzle(**changes)
    return snapshot_from_puzzles((puzzle,)).bind_target(puzzle.id)


def _target(**changes) -> PuzzleTarget:
    return _binding(**changes).target


def _utxo(
    *,
    value: int = 100_000,
    confirmed: bool = True,
    script_pubkey_hex: str = ADDRESS_SCRIPT,
) -> ChainUtxo:
    return ChainUtxo(
        txid="11" * 32,
        vout=0,
        value_sats=value,
        script_pubkey_hex=script_pubkey_hex,
        confirmed=confirmed,
    )


class _Transport:
    def __init__(self, utxos: tuple[ChainUtxo, ...], *, error: bool = False) -> None:
        self.utxos = utxos
        self.error = error

    def get(self, *, provider_id, resource, address):
        del address
        if self.error:
            return RawHttpResponse(503, b"unavailable")
        if resource is ProviderResource.TIP:
            key = "tip_height" if provider_id == FixtureProvider.ALPHA else "height"
            return RawHttpResponse(200, json.dumps({key: 900_000}).encode())
        if provider_id == FixtureProvider.ALPHA:
            payload = {
                "utxos": [
                    {
                        "txid": item.txid,
                        "vout": item.vout,
                        "value_sats": item.value_sats,
                        "script_pubkey_hex": item.script_pubkey_hex,
                        "confirmed": item.confirmed,
                    }
                    for item in self.utxos
                ]
            }
        else:
            payload = {
                "outputs": [
                    {
                        "transaction_id": item.txid,
                        "output_index": item.vout,
                        "satoshis": item.value_sats,
                        "locking_script": item.script_pubkey_hex,
                        "is_confirmed": item.confirmed,
                    }
                    for item in self.utxos
                ]
            }
        return RawHttpResponse(200, json.dumps(payload).encode())


def _receipt(
    *,
    target: PuzzleTarget | None = None,
    purpose: ChainPurpose = ChainPurpose.SELECTION,
    checked_at: datetime = NOW - timedelta(seconds=10),
    utxos: tuple[ChainUtxo, ...] = (_utxo(),),
    error: bool = False,
) -> ChainAdmissionReceipt:
    evidence = collect_chain_evidence(
        target=target or _target(),
        purpose=purpose,
        registry=ProviderRegistry.fixture(),
        transport=_Transport(utxos, error=error),
        clock=lambda: checked_at,
    )
    assert type(evidence) is ChainAdmissionReceipt
    return evidence


def _practice_evidence() -> tuple[CatalogTargetBinding, PracticeLookupBypass]:
    binding = snapshot_from_puzzles((get_puzzle(40),)).bind_target(40)
    assert binding.practice_fixture is not None
    evidence = collect_chain_evidence(
        target=binding.target,
        purpose=ChainPurpose.SELECTION,
        registry=ProviderRegistry.fixture(),
        transport=_Transport(()),
        clock=lambda: NOW,
        practice_fixture=binding.practice_fixture,
    )
    assert type(evidence) is PracticeLookupBypass
    return binding, evidence


def _full_catalog_practice_evidence(
    puzzle_id: int,
) -> tuple[CatalogTargetBinding, PracticeLookupBypass]:
    binding = FULL_CATALOG.bind_target(puzzle_id)
    assert binding.practice_fixture is not None
    evidence = collect_chain_evidence(
        target=binding.target,
        purpose=ChainPurpose.SELECTION,
        registry=ProviderRegistry.fixture(),
        transport=_Transport(()),
        clock=lambda: NOW,
        practice_fixture=binding.practice_fixture,
    )
    assert type(evidence) is PracticeLookupBypass
    return binding, evidence


def _host(
    *,
    cpus: int = 8,
    memory: int = 16 * GIB,
    gpus: tuple[GpuDevice, ...] = (),
) -> HostCapabilities:
    return HostCapabilities(
        architecture="x86_64",
        cpu_count=cpus,
        memory_bytes=memory,
        disk_free_bytes=100 * GIB,
        gpus=gpus,
    )


def _gpu(
    device_id: str = "0",
    *,
    memory: int = 8 * GIB,
    capability: tuple[int, int] | None = (8, 9),
    sms: int | None = 128,
) -> GpuDevice:
    return GpuDevice(
        device_id=device_id,
        name=f"GPU {device_id}",
        memory_bytes=memory,
        compute_capability=capability,
        multiprocessor_count=sms,
    )


def _assessment(result, engine: EngineName):
    return next(item for item in result.assessments if item.engine is engine)


def _codes(items) -> set[BlockerCode]:
    return {item.code for item in items}


def test_actual_range_not_display_bits_selects_builtin_with_exact_work():
    binding = _binding(end=100, bits_label=250)
    target = binding.target
    result = plan_target(binding, _receipt(target=target), _host(), evaluated_at=NOW)

    assert result.decision is not None
    assert result.decision.selected.engine is EngineName.SEQUENTIAL
    assert isinstance(result.decision.selected.recipe, SequentialRecipeV1)
    estimate = result.decision.selected.estimate
    assert estimate.full_work == estimate.horizon_work_limit == 100
    assert estimate.full_solution_expected_work == Fraction(101, 2)
    assert estimate.horizon_expected_occupied_work == Fraction(101, 2)
    assert estimate.horizon_hit_probability == Fraction(1, 1)
    assert isinstance(estimate.full_solution_eta_seconds, Fraction)
    assert isinstance(estimate.horizon_expected_occupied_seconds, Fraction)


def test_deterministic_probability_is_exactly_bounded_by_the_planning_horizon():
    binding = _binding(end=1 << 80)
    target = binding.target
    policy = PlanningPolicy(planning_horizon_seconds=60)
    result = plan_target(binding, _receipt(target=target), _host(), evaluated_at=NOW, policy=policy)
    estimate = _assessment(result, EngineName.KEYHUNT).estimate
    remaining = target.range_size
    planned = estimate.assumed_rate_per_second * policy.planning_horizon_seconds
    assert planned < remaining
    assert estimate.full_work == remaining
    assert estimate.horizon_work_limit == planned
    assert estimate.horizon_hit_probability == Fraction(planned, remaining)
    assert estimate.full_solution_expected_work == Fraction(remaining + 1, 2)
    assert estimate.horizon_expected_occupied_work == Fraction(
        planned * (2 * remaining - planned + 1), 2 * remaining
    )


def test_fastest_compares_full_eta_not_truncated_horizon_occupancy():
    binding = _binding(end=1 << 100, public_key=True)
    target = binding.target
    policy = PlanningPolicy(
        planning_horizon_seconds=60,
        allow_address_fallback_for_pubkey=True,
    )
    result = plan_target(binding, _receipt(target=target), _host(), evaluated_at=NOW, policy=policy)
    keyhunt = _assessment(result, EngineName.KEYHUNT)
    kangaroo = _assessment(result, EngineName.KANGAROO)

    assert keyhunt.viable and kangaroo.viable
    assert keyhunt.estimate.horizon_expected_occupied_seconds <= 60
    assert keyhunt.estimate.full_solution_eta_seconds > kangaroo.estimate.full_solution_eta_seconds
    assert result.decision.selected.engine is EngineName.KANGAROO


def test_gpu_address_target_assesses_all_adapters_and_selects_bitcrack():
    result = plan_target(
        _binding(),
        _receipt(),
        _host(gpus=(_gpu(),)),
        evaluated_at=NOW,
    )

    assert tuple(item.engine for item in result.assessments) == tuple(EngineName)
    assert result.decision.selected.engine is EngineName.BITCRACK
    recipe = result.decision.selected.recipe
    assert isinstance(recipe, BitCrackRecipeV1)
    assert recipe.device_id == "0"
    assert recipe.remaining_ranges == (_binding().target.key_range,)
    assert recipe.adapter_version == 1
    assert _assessment(result, EngineName.KEYHUNT).viable


def test_pubkey_gpu_defaults_to_auto_cpu_and_requires_opt_in_for_rck():
    binding = _binding(public_key=True)
    target = binding.target
    automatic = plan_target(
        binding,
        _receipt(target=target),
        _host(gpus=(_gpu(),)),
        evaluated_at=NOW,
    )
    rck = _assessment(automatic, EngineName.RCKANGAROO)

    assert automatic.decision.selected.engine is EngineName.KANGAROO
    assert automatic.decision.selected.provisioning is ProvisioningPolicy.AUTO_BUILD
    assert BlockerCode.MANUAL_PROVISIONING_DISABLED in _codes(rck.blockers)
    assert isinstance(rck.recipe, KangarooRecipeV1)
    assert rck.estimate is not None

    opted_in = plan_target(
        binding,
        _receipt(target=target),
        _host(gpus=(_gpu(),)),
        evaluated_at=NOW,
        policy=PlanningPolicy(allow_manual_provisioning=True),
    )

    selected = opted_in.decision.selected
    assert selected.engine is EngineName.RCKANGAROO
    assert selected.provisioning is ProvisioningPolicy.MANUAL_REQUIRED
    assert isinstance(selected.recipe, KangarooRecipeV1)
    assert selected.recipe.device_id == "0"
    assert selected.recipe.distinguished_point_bits_min == 14
    assert selected.recipe.distinguished_point_bits_max == 32
    assert selected.required_host_memory_floor_bytes == 410_517_504
    assert selected.required_device_memory_floor_bytes is not None
    assert selected.restart.value == "destructive"


@pytest.mark.parametrize(
    ("puzzle_id", "selected_engine"),
    [
        (21, EngineName.SEQUENTIAL),
        (22, EngineName.KEYHUNT),
        (31, EngineName.KEYHUNT),
        (32, EngineName.KANGAROO),
    ],
)
def test_full_catalog_practice_range_boundaries_keep_a_default_candidate(
    puzzle_id, selected_engine
):
    binding, evidence = _full_catalog_practice_evidence(puzzle_id)
    result = plan_target(binding, evidence, _host(), evaluated_at=NOW)

    assert result.decision is not None
    assert result.decision.selected.engine is selected_engine
    assert BlockerCode.NO_COMPATIBLE_ALGORITHM not in _codes(result.target_blockers)

    keyhunt = _assessment(result, EngineName.KEYHUNT)
    bitcrack = _assessment(result, EngineName.BITCRACK)
    if puzzle_id < 32:
        assert BlockerCode.ADDRESS_FALLBACK_DISABLED not in _codes(keyhunt.blockers)
        assert BlockerCode.ADDRESS_FALLBACK_DISABLED not in _codes(bitcrack.blockers)
    else:
        assert BlockerCode.ADDRESS_FALLBACK_DISABLED in _codes(keyhunt.blockers)
        assert BlockerCode.ADDRESS_FALLBACK_DISABLED in _codes(bitcrack.blockers)


@pytest.mark.parametrize("puzzle_id", [22, 31])
def test_small_pubkey_ranges_allow_address_gpu_family_when_kangaroo_cannot_run(puzzle_id):
    binding, evidence = _full_catalog_practice_evidence(puzzle_id)
    result = plan_target(
        binding,
        evidence,
        _host(gpus=(_gpu(),)),
        evaluated_at=NOW,
    )

    assert _assessment(result, EngineName.KEYHUNT).viable
    assert _assessment(result, EngineName.BITCRACK).viable
    assert BlockerCode.RANGE_UNSUPPORTED in _codes(
        _assessment(result, EngineName.KANGAROO).blockers
    )
    assert result.decision.selected.engine is EngineName.BITCRACK


def test_live_135_keeps_kangaroo_preferred_and_address_fallback_blocked():
    binding = FULL_CATALOG.bind_target(135)
    target = binding.target
    script_pubkey_hex = "76a914" + address_hash160(target.address).hex() + "88ac"
    receipt = _receipt(
        target=target,
        utxos=(_utxo(script_pubkey_hex=script_pubkey_hex),),
    )
    result = plan_target(binding, receipt, _host(), evaluated_at=NOW)

    assert result.decision is not None
    assert result.decision.selected.engine is EngineName.KANGAROO
    assert BlockerCode.ADDRESS_FALLBACK_DISABLED in _codes(
        _assessment(result, EngineName.KEYHUNT).blockers
    )
    assert BlockerCode.ADDRESS_FALLBACK_DISABLED in _codes(
        _assessment(result, EngineName.BITCRACK).blockers
    )


def test_zero_available_cpu_blocks_cpu_algorithms_but_not_supported_gpu():
    binding = _binding()
    result = plan_target(
        binding,
        _receipt(),
        _host(cpus=2, gpus=(_gpu(capability=(8, 0)),)),
        evaluated_at=NOW,
        policy=PlanningPolicy(cpu_reserved_cores=2),
    )
    assert result.decision.selected.engine is EngineName.BITCRACK
    for engine in (EngineName.SEQUENTIAL, EngineName.KEYHUNT, EngineName.KANGAROO):
        assessment = _assessment(result, engine)
        assert BlockerCode.CPU_CAPACITY_UNAVAILABLE in _codes(assessment.blockers)
        assert assessment.recipe is None
        assert assessment.estimate is None


def test_planner_does_not_probe_engine_inventory(monkeypatch):
    import btc_puzzle_lab.engines as engines

    monkeypatch.setattr(
        engines,
        "available_engines",
        lambda: (_ for _ in ()).throw(AssertionError("inventory probe")),
    )
    result = plan_target(_binding(), _receipt(), _host(), evaluated_at=NOW)
    assert result.decision.selected.engine is EngineName.KEYHUNT


def test_selection_uses_explicit_low_confidence_baselines():
    result = plan_target(_binding(), _receipt(), _host(), evaluated_at=NOW)
    for assessment in result.assessments:
        if assessment.estimate is None:
            continue
        assert assessment.estimate.source is EstimateSource.BASELINE
        assert assessment.estimate.confidence == Fraction(1, 10)
        assert "not measured on this host" in assessment.explanation


def test_raw_snapshot_cannot_be_promoted_by_the_planner():
    binding = _binding()
    snapshot = _receipt().snapshot
    with pytest.raises(PlanningValidationError, match="registered collection"):
        plan_target(binding, snapshot, _host(), evaluated_at=NOW)

    malformed = _AdapterReceipt(snapshot, "aa" * 32)
    with pytest.raises(PlanningValidationError, match="registered collection"):
        plan_target(binding, malformed, _host(), evaluated_at=NOW)


def test_exact_catalog_binding_is_required_and_cannot_be_rebound():
    binding = _binding()
    receipt = _receipt(target=binding.target)
    with pytest.raises(PlanningValidationError, match="catalog binding"):
        plan_target(binding.target, receipt, _host(), evaluated_at=NOW)

    other = _binding(puzzle_id=72)
    borrowed = plan_target(other, receipt, _host(), evaluated_at=NOW)
    assert borrowed.decision is None
    assert BlockerCode.CHAIN_TARGET_MISMATCH in _codes(borrowed.target_blockers)
    with pytest.raises(CatalogTargetError, match="catalog snapshot"):
        replace(binding, target=other.target)
    with pytest.raises(CatalogTargetError, match="catalog snapshot"):
        replace(binding, catalog_fingerprint=other.catalog_fingerprint)


def test_exact_type_forgery_cannot_mint_a_selection_decision():
    binding = _binding()
    receipt = _receipt(target=binding.target)

    forged_binding = object.__new__(CatalogTargetBinding)
    for name in ("target", "practice_fixture", "catalog_fingerprint"):
        object.__setattr__(forged_binding, name, getattr(binding, name))
    with pytest.raises(PlanningValidationError, match="issued by an unchanged"):
        plan_target(forged_binding, receipt, _host(), evaluated_at=NOW)

    forged_receipt = object.__new__(ChainAdmissionReceipt)
    for name in ("target", "snapshot", "receipt_fingerprint"):
        object.__setattr__(forged_receipt, name, getattr(receipt, name))
    with pytest.raises(PlanningValidationError, match="unchanged from registered"):
        plan_target(binding, forged_receipt, _host(), evaluated_at=NOW)


def test_planner_rejects_host_policy_datetime_subtypes_and_decision_is_final():
    class FakeHost(HostCapabilities):
        pass

    class FakePolicy(PlanningPolicy):
        pass

    class FakeDatetime(datetime):
        pass

    binding = _binding()
    fake_host = FakeHost(architecture="x86_64", cpu_count=8, memory_bytes=16 * GIB)
    with pytest.raises(PlanningValidationError, match="typed facts"):
        plan_target(binding, _receipt(), fake_host, evaluated_at=NOW)
    with pytest.raises(PlanningValidationError, match="PlanningPolicy"):
        plan_target(binding, _receipt(), _host(), evaluated_at=NOW, policy=FakePolicy())
    with pytest.raises(PlanningValidationError, match="datetime"):
        plan_target(
            binding,
            _receipt(),
            _host(),
            evaluated_at=FakeDatetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        )
    with pytest.raises(TypeError, match="final"):

        class FakeDecision(SelectionDecision):
            pass


@pytest.mark.parametrize(
    ("receipt", "code"),
    [
        (_receipt(utxos=()), BlockerCode.PRIZE_SWEPT),
        (_receipt(utxos=(_utxo(confirmed=False),)), BlockerCode.PRIZE_UNCONFIRMED),
        (_receipt(error=True), BlockerCode.PRIZE_UNKNOWN),
        (
            _receipt(checked_at=NOW - timedelta(seconds=301)),
            BlockerCode.CHAIN_EVIDENCE_STALE,
        ),
        (
            _receipt(checked_at=NOW + timedelta(seconds=1)),
            BlockerCode.CHAIN_EVIDENCE_STALE,
        ),
        (_receipt(target=_target(puzzle_id=72)), BlockerCode.CHAIN_TARGET_MISMATCH),
        (
            _receipt(target=_target(end=(1 << 40) + 1)),
            BlockerCode.CHAIN_TARGET_MISMATCH,
        ),
        (
            _receipt(target=_target(public_key=True)),
            BlockerCode.CHAIN_TARGET_MISMATCH,
        ),
        (
            _receipt(purpose=ChainPurpose.LAUNCH),
            BlockerCode.CHAIN_PURPOSE_MISMATCH,
        ),
    ],
)
def test_live_chain_gates_fail_closed(receipt, code):
    result = plan_target(_binding(), receipt, _host(), evaluated_at=NOW)
    assert result.decision is None
    assert code in _codes(result.target_blockers)


def test_live_and_practice_require_exact_sealed_evidence_types():
    with pytest.raises(PlanningValidationError, match="registered collection"):
        plan_target(_binding(), None, _host(), evaluated_at=NOW)

    practice_binding, practice_bypass = _practice_evidence()
    practice = plan_target(practice_binding, practice_bypass, _host(), evaluated_at=NOW)
    assert practice.decision is not None
    wrong_for_practice = plan_target(practice_binding, _receipt(), _host(), evaluated_at=NOW)
    assert BlockerCode.PRACTICE_EVIDENCE_REQUIRED in _codes(wrong_for_practice.target_blockers)
    wrong_for_live = plan_target(_binding(), practice_bypass, _host(), evaluated_at=NOW)
    assert BlockerCode.CHAIN_EVIDENCE_REQUIRED in _codes(wrong_for_live.target_blockers)


def test_rck_source_floors_reject_impossible_gpu_and_host_memory():
    binding = _binding(public_key=True)
    gpu_too_small = plan_target(
        binding,
        _receipt(target=binding.target),
        _host(gpus=(_gpu(memory=1),)),
        evaluated_at=NOW,
        policy=PlanningPolicy(allow_manual_provisioning=True),
    )
    rck = _assessment(gpu_too_small, EngineName.RCKANGAROO)
    assert BlockerCode.GPU_MEMORY_INSUFFICIENT in _codes(rck.blockers)
    assert rck.required_device_memory_floor_bytes > 1
    assert rck.required_host_memory_floor_bytes == 410_517_504

    host_too_small = plan_target(
        binding,
        _receipt(target=binding.target),
        _host(memory=1, gpus=(_gpu(),)),
        evaluated_at=NOW,
        policy=PlanningPolicy(allow_manual_provisioning=True),
    )
    rck = _assessment(host_too_small, EngineName.RCKANGAROO)
    assert BlockerCode.HOST_MEMORY_INSUFFICIENT in _codes(rck.blockers)
    assert rck.required_host_memory_floor_bytes == 410_517_504
    assert rck.required_device_memory_floor_bytes > 1

    for engine in (
        EngineName.SEQUENTIAL,
        EngineName.KEYHUNT,
        EngineName.KANGAROO,
        EngineName.BITCRACK,
    ):
        assessment = _assessment(host_too_small, engine)
        assert assessment.required_host_memory_floor_bytes is None
        assert assessment.required_device_memory_floor_bytes is None


def test_unknown_gpu_capability_blocks_bitcrack():
    unknown_gpu = plan_target(
        _binding(),
        _receipt(),
        _host(gpus=(_gpu(capability=None),)),
        evaluated_at=NOW,
    )
    assert BlockerCode.GPU_CAPABILITY_UNKNOWN in _codes(
        _assessment(unknown_gpu, EngineName.BITCRACK).blockers
    )


def test_gpu_choice_uses_a_supported_device_and_is_deterministic():
    host = _host(
        gpus=(
            _gpu("unknown-large", memory=32 * GIB, capability=None),
            _gpu("supported-b", memory=8 * GIB, capability=(8, 0)),
            _gpu("supported-a", memory=8 * GIB, capability=(8, 0)),
        ),
    )
    result = plan_target(_binding(), _receipt(), host, evaluated_at=NOW)
    assert result.decision.selected.recipe.device_id == "supported-a"


def test_rck_skips_invalid_topology_and_selects_a_supported_gpu():
    binding = _binding(public_key=True)
    host = _host(
        gpus=(
            _gpu("unknown-large", memory=32 * GIB, sms=None),
            _gpu("valid", memory=8 * GIB, sms=80),
        )
    )
    result = plan_target(
        binding,
        _receipt(target=binding.target),
        host,
        evaluated_at=NOW,
        policy=PlanningPolicy(allow_manual_provisioning=True),
    )
    assert result.decision.selected.engine is EngineName.RCKANGAROO
    assert result.decision.selected.recipe.device_id == "valid"


def test_rck_range_cap_is_based_on_actual_range_not_bits_label():
    binding = _binding(end=1 << 171, public_key=True, bits_label=40)
    target = binding.target
    result = plan_target(
        binding,
        _receipt(target=target),
        _host(gpus=(_gpu(),)),
        evaluated_at=NOW,
    )
    assert BlockerCode.RANGE_UNSUPPORTED in _codes(
        _assessment(result, EngineName.RCKANGAROO).blockers
    )
    assert result.decision.selected.engine is EngineName.KANGAROO


def test_rck_requires_power_of_two_range_and_pinned_v4_gpu_capability():
    uneven = _binding(end=(1 << 40) + 1, public_key=True)
    uneven_result = plan_target(
        uneven,
        _receipt(target=uneven.target),
        _host(gpus=(_gpu(capability=(8, 9)),)),
        evaluated_at=NOW,
    )
    assert BlockerCode.RANGE_UNSUPPORTED in _codes(
        _assessment(uneven_result, EngineName.RCKANGAROO).blockers
    )

    power_of_two = _binding(public_key=True)
    unsupported_result = plan_target(
        power_of_two,
        _receipt(target=power_of_two.target),
        _host(gpus=(_gpu(capability=(8, 0)),)),
        evaluated_at=NOW,
    )
    assert BlockerCode.GPU_CAPABILITY_UNSUPPORTED in _codes(
        _assessment(unsupported_result, EngineName.RCKANGAROO).blockers
    )


def test_decision_fingerprint_is_derived_from_canonical_facts_recipe_and_policy():
    binding = _binding()
    host = _host(gpus=(_gpu(),))
    first = plan_target(binding, _receipt(), host, evaluated_at=NOW).decision
    same = plan_target(binding, _receipt(), host, evaluated_at=NOW).decision
    assert first.decision_fingerprint == same.decision_fingerprint

    changed_host = plan_target(
        binding,
        _receipt(),
        replace(host, cpu_count=4),
        evaluated_at=NOW,
    ).decision
    changed_policy = plan_target(
        binding,
        _receipt(),
        host,
        evaluated_at=NOW,
        policy=PlanningPolicy(planning_horizon_seconds=3_600),
    ).decision
    changed_chain = plan_target(
        binding,
        _receipt(utxos=(_utxo(value=200_000),)),
        host,
        evaluated_at=NOW,
    ).decision
    assert len(first.decision_fingerprint) == 64
    assert (
        len(
            {
                first.decision_fingerprint,
                changed_host.decision_fingerprint,
                changed_policy.decision_fingerprint,
                changed_chain.decision_fingerprint,
            }
        )
        == 4
    )


def test_non_target_catalog_change_invalidates_the_selection_decision():
    selected = _puzzle()
    other = _puzzle(
        puzzle_id=72,
        end=2,
        bits_label=2,
        address="1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb",
    )
    original = snapshot_from_puzzles((selected, other)).bind_target(selected.id)
    changed = snapshot_from_puzzles(
        (selected, replace(other, notes="changed non-target metadata"))
    ).bind_target(selected.id)
    receipt = _receipt(target=original.target)

    original_decision = plan_target(original, receipt, _host(), evaluated_at=NOW).decision
    changed_decision = plan_target(changed, receipt, _host(), evaluated_at=NOW).decision
    assert original.target == changed.target
    assert original.catalog_fingerprint != changed.catalog_fingerprint
    assert original_decision.catalog_fingerprint == original.catalog_fingerprint
    assert changed_decision.catalog_fingerprint == changed.catalog_fingerprint
    assert original_decision.decision_fingerprint != changed_decision.decision_fingerprint


def test_recipe_records_are_frozen_and_validate_parameters():
    binding = _binding(end=100)
    target = binding.target
    recipe = plan_target(
        binding, _receipt(target=target), _host(), evaluated_at=NOW
    ).decision.selected.recipe
    with pytest.raises(FrozenInstanceError):
        recipe.remaining_ranges = ()
    with pytest.raises(PlanningValidationError, match="ordered"):
        SequentialRecipeV1(
            remaining_ranges=(KeyRange(start=2, end=3), KeyRange(start=1, end=1)),
        )
    with pytest.raises(PlanningValidationError, match="ordered"):
        KeyhuntRecipeV1(
            remaining_ranges=(KeyRange(start=2, end=3), KeyRange(start=1, end=1)),
        )
    with pytest.raises(PlanningValidationError, match="device_id"):
        BitCrackRecipeV1(
            device_id=" ",
            remaining_ranges=(KeyRange(start=1, end=2),),
        )
    with pytest.raises(PlanningValidationError, match="ordered"):
        BitCrackRecipeV1(
            device_id="0",
            remaining_ranges=(KeyRange(start=2, end=3), KeyRange(start=1, end=1)),
        )
    with pytest.raises(PlanningValidationError, match="plan_target"):
        replace(plan_target(_binding(), _receipt(), _host(), evaluated_at=NOW).decision)


@pytest.mark.parametrize(
    "policy",
    [
        PlanningPolicy(memory_safety_fraction=Fraction(1, 1)),
        PlanningPolicy(cpu_reserved_cores=10),
        PlanningPolicy(planning_horizon_seconds=1),
        PlanningPolicy(allow_manual_provisioning=True),
    ],
)
def test_valid_explicit_policy_values_are_fingerprinted(policy):
    assert len(policy.policy_fingerprint) == 64


def test_policy_rejects_float_fraction_and_invalid_bool():
    with pytest.raises(PlanningValidationError, match="Fraction"):
        PlanningPolicy(memory_safety_fraction=0.75)
    with pytest.raises(PlanningValidationError, match="boolean"):
        PlanningPolicy(allow_address_fallback_for_pubkey=1)
    with pytest.raises(PlanningValidationError, match="boolean"):
        PlanningPolicy(allow_manual_provisioning=1)
