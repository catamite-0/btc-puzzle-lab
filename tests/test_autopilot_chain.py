import copy
import json
import pickle
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import btc_puzzle_lab.autopilot.chain as chain_mod
from btc_puzzle_lab.autopilot.catalog_view import PracticeFixtureEvidence, entry_from_puzzle
from btc_puzzle_lab.autopilot.chain import (
    ChainAcquisitionError,
    ChainAdmissionReceipt,
    ChainEvidenceProvenance,
    EsploraAdapter,
    FixtureAlphaAdapter,
    FixtureBetaAdapter,
    FixtureProvider,
    HttpChainTransport,
    HttpTransportError,
    PracticeLookupBypass,
    ProductionProvider,
    ProviderPayloadError,
    ProviderRegistry,
    ProviderResource,
    RawHttpResponse,
    collect_chain_evidence,
    collect_production_chain_evidence,
    is_chain_admission_receipt_issued,
    is_practice_lookup_bypass_issued,
    is_production_chain_admission_receipt_issued,
    is_provider_registry_issued,
)
from btc_puzzle_lab.autopilot.facts import (
    MAX_BITCOIN_SUPPLY_SATS,
    ChainPurpose,
    ChainSnapshot,
    ChainState,
    KeyRange,
    ProviderAuthority,
    ProviderOutcome,
    PuzzleTarget,
    TargetMode,
)
from btc_puzzle_lab.catalog import Puzzle

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
ADDRESS = "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
ADDRESS_SCRIPT = "76a914751e76e8199196d454941c45d1b3a323f1433bd688ac"
TXID = "11" * 32
BLOCK_HASH = "22" * 32


def _target(*, mode: TargetMode = TargetMode.LIVE) -> PuzzleTarget:
    return PuzzleTarget(
        puzzle_id=71,
        key_range=KeyRange(start=1, end=100),
        address=ADDRESS,
        mode=mode,
        practice_fixture_id="public-fixture-71" if mode is TargetMode.PRACTICE else None,
    )


def _practice_entry():
    return entry_from_puzzle(
        Puzzle(
            id=1,
            bits=1,
            address=ADDRESS,
            range_start=1,
            range_end=1,
            pubkey_compressed_hex=(
                "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
            ),
            practice_solution=1,
            status="solved",
            engine_default="sequential",
            notes="public fixture",
        )
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _alpha_utxos(
    *,
    value_sats: int = 100_000,
    confirmed: bool = True,
    script_pubkey_hex: str = ADDRESS_SCRIPT,
) -> bytes:
    return _json_bytes(
        {
            "utxos": [
                {
                    "confirmed": confirmed,
                    "script_pubkey_hex": script_pubkey_hex,
                    "txid": TXID,
                    "value_sats": value_sats,
                    "vout": 0,
                }
            ]
        }
    )


def _beta_utxos(
    *,
    value_sats: int = 100_000,
    confirmed: bool = True,
    script_pubkey_hex: str = ADDRESS_SCRIPT,
) -> bytes:
    return _json_bytes(
        {
            "outputs": [
                {
                    "is_confirmed": confirmed,
                    "locking_script": script_pubkey_hex,
                    "output_index": 0,
                    "satoshis": value_sats,
                    "transaction_id": TXID,
                }
            ]
        }
    )


def _esplora_status(*, confirmed: bool = True) -> dict[str, object]:
    if not confirmed:
        return {"confirmed": False}
    return {
        "block_hash": BLOCK_HASH,
        "block_height": 900_000,
        "block_time": 1_700_000_000,
        "confirmed": True,
    }


def _esplora_utxos(
    *,
    txid: str = TXID,
    value_sats: int = 100_000,
    confirmed: bool = True,
    vout: int = 0,
) -> bytes:
    return _json_bytes(
        [
            {
                "status": _esplora_status(confirmed=confirmed),
                "txid": txid,
                "value": value_sats,
                "vout": vout,
            }
        ]
    )


def _esplora_transaction(
    *,
    txid: str = TXID,
    value_sats: int = 100_000,
    script_pubkey_hex: str = ADDRESS_SCRIPT,
    confirmed: bool = True,
    vout: int = 0,
) -> bytes:
    outputs = [{"scriptpubkey": "51", "value": 1} for _ in range(vout + 1)]
    outputs[vout] = {
        "scriptpubkey": script_pubkey_hex,
        "value": value_sats,
    }
    return _json_bytes(
        {
            "status": _esplora_status(confirmed=confirmed),
            "txid": txid,
            "vout": outputs,
        }
    )


class _Clock:
    def __init__(self, values: Iterable[datetime]) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class _Transport:
    def __init__(self, responses: dict[tuple[str, ProviderResource], object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ProviderResource, str]] = []

    def get(
        self,
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
    ) -> RawHttpResponse:
        self.calls.append((provider_id, resource, address))
        response = self.responses[(provider_id, resource)]
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


class _ProductionTransport:
    def __init__(
        self,
        responses: dict[tuple[str, ProviderResource, str | None], object],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ProviderResource, str, str | None]] = []

    def get(
        self,
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
        txid: str | None = None,
    ) -> RawHttpResponse:
        self.calls.append((provider_id, resource, address, txid))
        response = self.responses[(provider_id, resource, txid)]
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def _responses(
    *,
    alpha_utxos: bytes | None = None,
    beta_utxos: bytes | None = None,
    alpha_tip: int = 900_000,
    beta_tip: int = 900_002,
) -> dict[tuple[str, ProviderResource], object]:
    return {
        ("fixture-alpha", ProviderResource.UTXOS): RawHttpResponse(
            200,
            alpha_utxos if alpha_utxos is not None else _alpha_utxos(),
        ),
        ("fixture-alpha", ProviderResource.TIP): RawHttpResponse(
            200,
            _json_bytes({"tip_height": alpha_tip}),
        ),
        ("fixture-beta", ProviderResource.UTXOS): RawHttpResponse(
            200,
            beta_utxos if beta_utxos is not None else _beta_utxos(),
        ),
        ("fixture-beta", ProviderResource.TIP): RawHttpResponse(
            200,
            _json_bytes({"height": beta_tip}),
        ),
    }


def _production_responses(
    *,
    mempool_utxos: bytes | None = None,
    blockstream_utxos: bytes | None = None,
    mempool_transaction: bytes | None = None,
    blockstream_transaction: bytes | None = None,
    mempool_tip: int = 900_000,
    blockstream_tip: int = 900_002,
) -> dict[tuple[str, ProviderResource, str | None], object]:
    return {
        ("mempool-space", ProviderResource.UTXOS, None): RawHttpResponse(
            200,
            mempool_utxos if mempool_utxos is not None else _esplora_utxos(),
        ),
        ("mempool-space", ProviderResource.TIP, None): RawHttpResponse(
            200,
            f"{mempool_tip}\n".encode("ascii"),
        ),
        ("mempool-space", ProviderResource.TRANSACTION, TXID): RawHttpResponse(
            200,
            (mempool_transaction if mempool_transaction is not None else _esplora_transaction()),
        ),
        ("blockstream-info", ProviderResource.UTXOS, None): RawHttpResponse(
            200,
            blockstream_utxos if blockstream_utxos is not None else _esplora_utxos(),
        ),
        ("blockstream-info", ProviderResource.TIP, None): RawHttpResponse(
            200,
            f"{blockstream_tip}\n".encode("ascii"),
        ),
        ("blockstream-info", ProviderResource.TRANSACTION, TXID): RawHttpResponse(
            200,
            (
                blockstream_transaction
                if blockstream_transaction is not None
                else _esplora_transaction()
            ),
        ),
    }


def _collect(
    transport: _Transport,
    *,
    registry: ProviderRegistry | None = None,
    purpose: ChainPurpose = ChainPurpose.SELECTION,
    times: tuple[datetime, datetime, datetime] = (NOW, NOW, NOW),
) -> ChainSnapshot:
    evidence = collect_chain_evidence(
        target=_target(),
        purpose=purpose,
        registry=registry or ProviderRegistry.fixture(),
        transport=transport,
        clock=_Clock(times),
    )
    assert type(evidence) is ChainAdmissionReceipt
    return evidence.snapshot


def _collect_production(
    transport: _ProductionTransport,
    *,
    registry: ProviderRegistry | None = None,
) -> ChainSnapshot:
    evidence = collect_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
        registry=registry or ProviderRegistry.production(),
        transport=transport,
        clock=_Clock((NOW, NOW, NOW)),
    )
    assert type(evidence) is ChainAdmissionReceipt
    return evidence.snapshot


def test_independent_registry_provenance_and_full_utxo_quorum_are_normalized():
    transport = _Transport(_responses())
    snapshot = _collect(transport)

    assert snapshot.state is ChainState.FUNDED_CONFIRMED
    assert snapshot.confirmed_sats == 100_000
    assert snapshot.unconfirmed_sats == 0
    assert {observation.provider_id for observation in snapshot.observations} == {
        "fixture-alpha",
        "fixture-beta",
    }
    assert {observation.authority for observation in snapshot.observations} == {
        ProviderAuthority.PUBLIC
    }
    assert {observation.independence_group for observation in snapshot.observations} == {
        "fixture-backend-alpha",
        "fixture-backend-beta",
    }
    assert transport.calls == [
        ("fixture-alpha", ProviderResource.UTXOS, ADDRESS),
        ("fixture-alpha", ProviderResource.TIP, ADDRESS),
        ("fixture-beta", ProviderResource.UTXOS, ADDRESS),
        ("fixture-beta", ProviderResource.TIP, ADDRESS),
    ]


def test_two_providers_can_agree_that_target_is_empty_or_only_unconfirmed():
    empty = _responses(
        alpha_utxos=_json_bytes({"utxos": []}), beta_utxos=_json_bytes({"outputs": []})
    )
    assert _collect(_Transport(empty)).state is ChainState.EMPTY

    unconfirmed = _responses(
        alpha_utxos=_alpha_utxos(confirmed=False),
        beta_utxos=_beta_utxos(confirmed=False),
    )
    snapshot = _collect(_Transport(unconfirmed))
    assert snapshot.state is ChainState.FUNDED_UNCONFIRMED
    assert snapshot.confirmed_sats == 0
    assert snapshot.unconfirmed_sats == 100_000


def test_one_public_provider_never_establishes_chain_state():
    registry = ProviderRegistry.fixture((FixtureProvider.ALPHA,))
    responses = _responses()
    transport = _Transport(responses)
    snapshot = _collect(
        transport,
        registry=registry,
        times=(NOW, NOW, NOW),
    )

    assert snapshot.state is ChainState.UNKNOWN
    assert snapshot.unknown_reason == "independent_provider_quorum_missing"
    assert transport.calls == [
        ("fixture-alpha", ProviderResource.UTXOS, ADDRESS),
        ("fixture-alpha", ProviderResource.TIP, ADDRESS),
    ]


@pytest.mark.parametrize(
    ("responses", "reason"),
    [
        (
            _responses(beta_utxos=_beta_utxos(value_sats=99_999)),
            "provider_utxo_disagreement",
        ),
        (
            _responses(alpha_tip=900_000, beta_tip=900_003),
            "provider_tip_disagreement",
        ),
    ],
)
def test_provider_utxo_or_tip_disagreement_is_unknown(responses, reason):
    snapshot = _collect(_Transport(responses))
    assert snapshot.state is ChainState.UNKNOWN
    assert snapshot.unknown_reason == reason


def test_http_or_transport_failure_is_an_error_observation_and_unknown():
    responses = _responses()
    responses[("fixture-beta", ProviderResource.UTXOS)] = RawHttpResponse(503, b"unavailable")
    snapshot = _collect(_Transport(responses))
    beta = next(item for item in snapshot.observations if item.provider_id == "fixture-beta")

    assert snapshot.state is ChainState.UNKNOWN
    assert snapshot.unknown_reason == "provider_error"
    assert beta.outcome is ProviderOutcome.ERROR
    assert beta.error_code == "utxos_http_503"
    assert not beta.utxos and beta.tip_height is None

    responses = _responses()
    responses[("fixture-alpha", ProviderResource.TIP)] = TimeoutError("fixture timeout")
    snapshot = _collect(_Transport(responses))
    alpha = next(item for item in snapshot.observations if item.provider_id == "fixture-alpha")
    assert snapshot.state is ChainState.UNKNOWN
    assert alpha.error_code == "tip_transport_error"


@pytest.mark.parametrize(
    "bad_payload",
    [
        b"not-json",
        b'{"utxos":[],"utxos":[]}',
        b'{"utxos":[],"authority":"trusted_node"}',
        _json_bytes({"utxos": [{"confirmed": True}]}),
        _json_bytes({"utxos": [{"confirmed": 1}]}),
    ],
)
def test_malformed_or_metadata_spoofing_payload_fails_closed(bad_payload):
    snapshot = _collect(_Transport(_responses(alpha_utxos=bad_payload)))
    alpha = next(item for item in snapshot.observations if item.provider_id == "fixture-alpha")

    assert snapshot.state is ChainState.UNKNOWN
    assert alpha.outcome is ProviderOutcome.ERROR
    assert alpha.error_code == "utxos_invalid_payload"
    assert alpha.authority is ProviderAuthority.PUBLIC
    assert alpha.independence_group == "fixture-backend-alpha"


def test_malformed_tip_and_transport_result_fail_closed():
    responses = _responses()
    responses[("fixture-beta", ProviderResource.TIP)] = RawHttpResponse(
        200,
        _json_bytes({"height": True}),
    )
    snapshot = _collect(_Transport(responses))
    beta = next(item for item in snapshot.observations if item.provider_id == "fixture-beta")
    assert snapshot.state is ChainState.UNKNOWN
    assert beta.error_code == "tip_invalid_payload"

    responses = _responses()
    responses[("fixture-alpha", ProviderResource.UTXOS)] = object()
    snapshot = _collect(_Transport(responses))
    alpha = next(item for item in snapshot.observations if item.provider_id == "fixture-alpha")
    assert snapshot.state is ChainState.UNKNOWN
    assert alpha.error_code == "utxos_invalid_response"


def test_wrong_address_locking_script_is_unknown_not_funded():
    responses = _responses(alpha_utxos=_alpha_utxos(script_pubkey_hex="51"))
    snapshot = _collect(_Transport(responses))
    alpha = next(item for item in snapshot.observations if item.provider_id == "fixture-alpha")

    assert snapshot.state is ChainState.UNKNOWN
    assert snapshot.unknown_reason == "provider_error"
    assert alpha.outcome is ProviderOutcome.ERROR
    assert alpha.error_code == "utxo_address_mismatch"
    assert not alpha.utxos


def test_future_or_stale_local_observation_is_replaced_by_error_evidence():
    future = _collect(
        _Transport(_responses()),
        times=(NOW + timedelta(seconds=1), NOW, NOW),
    )
    alpha = next(item for item in future.observations if item.provider_id == "fixture-alpha")
    assert future.state is ChainState.UNKNOWN
    assert alpha.outcome is ProviderOutcome.ERROR
    assert alpha.error_code == "future_observation"

    stale_at = NOW + timedelta(seconds=61)
    stale = _collect(
        _Transport(_responses()),
        purpose=ChainPurpose.LAUNCH,
        times=(NOW, NOW, stale_at),
    )
    assert stale.state is ChainState.UNKNOWN
    assert {item.error_code for item in stale.observations} == {"stale_observation"}
    assert not stale.is_fresh(stale_at)


def test_practice_target_bypasses_transport_without_creating_chain_state():
    transport = _Transport({})
    entry = _practice_entry()
    assert entry.practice_fixture is not None
    evidence = collect_chain_evidence(
        target=entry.target,
        purpose=ChainPurpose.SELECTION,
        registry=ProviderRegistry.fixture(),
        transport=transport,
        clock=lambda: (_ for _ in ()).throw(AssertionError("clock must not be read")),
        practice_fixture=entry.practice_fixture,
    )

    assert isinstance(evidence, PracticeLookupBypass)
    assert evidence.provenance is ChainEvidenceProvenance.CATALOG_PRACTICE_V1
    assert evidence.target.practice_fixture_id == "package-catalog-v1:puzzle-1"
    assert not hasattr(evidence, "state")
    assert not hasattr(evidence, "confirmed_sats")
    assert transport.calls == []


def test_live_target_cannot_construct_or_borrow_practice_bypass():
    fixture = _practice_entry().practice_fixture
    assert fixture is not None
    with pytest.raises(ChainAcquisitionError, match="registered collection"):
        PracticeLookupBypass(
            target=_target(),
            purpose=ChainPurpose.SELECTION,
            fixture=fixture,
        )

    target = _target()
    receipt = collect_chain_evidence(
        target=target,
        purpose=ChainPurpose.SELECTION,
        registry=ProviderRegistry.fixture(),
        transport=_Transport(_responses()),
        clock=_Clock((NOW, NOW, NOW)),
    )
    assert type(receipt) is ChainAdmissionReceipt
    assert receipt.provenance is ChainEvidenceProvenance.FIXTURE_V1
    assert receipt.target == target
    assert receipt.snapshot.state is ChainState.FUNDED_CONFIRMED
    assert len(receipt.receipt_fingerprint) == 64


def test_live_receipt_fingerprint_binds_the_complete_target():
    target = _target()
    changed_range = replace(
        target,
        key_range=KeyRange(start=target.key_range.start, end=target.key_range.end - 1),
    )

    def collect(bound_target: PuzzleTarget) -> ChainAdmissionReceipt:
        receipt = collect_chain_evidence(
            target=bound_target,
            purpose=ChainPurpose.SELECTION,
            registry=ProviderRegistry.fixture(),
            transport=_Transport(_responses()),
            clock=_Clock((NOW, NOW, NOW)),
        )
        assert type(receipt) is ChainAdmissionReceipt
        return receipt

    original = collect(target)
    changed = collect(changed_range)
    assert original.snapshot.evidence_fingerprint == changed.snapshot.evidence_fingerprint
    assert original.receipt_fingerprint != changed.receipt_fingerprint


def test_caller_asserted_practice_mode_cannot_bypass_live_lookup():
    forged = replace(
        _target(),
        mode=TargetMode.PRACTICE,
        practice_fixture_id="caller-asserted-practice",
    )
    transport = _Transport({})

    with pytest.raises(ChainAcquisitionError, match="catalog-verified"):
        collect_chain_evidence(
            target=forged,
            purpose=ChainPurpose.SELECTION,
            registry=ProviderRegistry.fixture(),
            transport=transport,
            clock=lambda: NOW,
        )

    assert transport.calls == []


def test_practice_fixture_subclass_cannot_override_target_binding():
    class ForgedFixture(PracticeFixtureEvidence):
        def matches(self, _target):
            return True

    forged_fixture = object.__new__(ForgedFixture)
    object.__setattr__(forged_fixture, "target", _practice_entry().target)
    object.__setattr__(forged_fixture, "fixture_fingerprint", "a" * 64)
    forged_target = replace(
        _target(),
        mode=TargetMode.PRACTICE,
        practice_fixture_id="caller-asserted-practice",
    )

    with pytest.raises(ChainAcquisitionError, match="registered collection"):
        PracticeLookupBypass(
            target=forged_target,
            purpose=ChainPurpose.SELECTION,
            fixture=forged_fixture,
        )


def test_provider_registry_subclass_cannot_override_provenance():
    class CallerRegistry(ProviderRegistry):
        pass

    caller_registry = object.__new__(CallerRegistry)
    object.__setattr__(caller_registry, "_provider_ids", ())
    with pytest.raises(ChainAcquisitionError, match="ProviderRegistry"):
        collect_chain_evidence(
            target=_target(),
            purpose=ChainPurpose.SELECTION,
            registry=caller_registry,
            transport=_Transport({}),
            clock=lambda: NOW,
        )
    assert type(CallerRegistry.fixture()) is ProviderRegistry


def test_chain_authority_objects_require_unchanged_process_local_issuance():
    registry = ProviderRegistry.fixture()
    receipt = collect_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
        registry=registry,
        transport=_Transport(_responses()),
        clock=_Clock((NOW, NOW, NOW)),
    )
    entry = _practice_entry()
    assert entry.practice_fixture is not None
    bypass = collect_chain_evidence(
        target=entry.target,
        purpose=ChainPurpose.SELECTION,
        registry=ProviderRegistry.fixture(),
        transport=_Transport({}),
        clock=lambda: NOW,
        practice_fixture=entry.practice_fixture,
    )
    assert type(receipt) is ChainAdmissionReceipt
    assert type(bypass) is PracticeLookupBypass
    assert is_provider_registry_issued(registry)
    assert is_chain_admission_receipt_issued(receipt)
    assert is_practice_lookup_bypass_issued(bypass)

    forged_registry = object.__new__(ProviderRegistry)
    object.__setattr__(forged_registry, "_provider_ids", registry._provider_ids)
    forged_receipt = object.__new__(ChainAdmissionReceipt)
    object.__setattr__(forged_receipt, "target", receipt.target)
    object.__setattr__(forged_receipt, "snapshot", receipt.snapshot)
    object.__setattr__(forged_receipt, "provenance", receipt.provenance)
    object.__setattr__(forged_receipt, "receipt_fingerprint", receipt.receipt_fingerprint)
    forged_bypass = object.__new__(PracticeLookupBypass)
    object.__setattr__(forged_bypass, "target", bypass.target)
    object.__setattr__(forged_bypass, "purpose", bypass.purpose)
    object.__setattr__(forged_bypass, "fixture", bypass.fixture)
    object.__setattr__(forged_bypass, "provenance", bypass.provenance)
    object.__setattr__(forged_bypass, "receipt_fingerprint", bypass.receipt_fingerprint)
    assert not is_provider_registry_issued(forged_registry)
    assert not is_chain_admission_receipt_issued(forged_receipt)
    assert not is_practice_lookup_bypass_issued(forged_bypass)

    for original, validator in (
        (registry, is_provider_registry_issued),
        (receipt, is_chain_admission_receipt_issued),
        (bypass, is_practice_lookup_bypass_issued),
    ):
        assert not validator(copy.deepcopy(original))
        assert not validator(pickle.loads(pickle.dumps(original)))
        with pytest.raises((TypeError, ChainAcquisitionError)):
            replace(original)

    object.__setattr__(registry, "_provider_ids", (FixtureProvider.ALPHA,))
    object.__setattr__(receipt, "receipt_fingerprint", "f" * 64)
    object.__setattr__(bypass, "receipt_fingerprint", "e" * 64)
    assert not is_provider_registry_issued(registry)
    assert not is_chain_admission_receipt_issued(receipt)
    assert not is_practice_lookup_bypass_issued(bypass)


def test_slow_provider_ages_early_utxo_evidence_from_request_start():
    snapshot = _collect(
        _Transport(_responses()),
        times=(NOW, NOW + timedelta(seconds=600), NOW + timedelta(seconds=600)),
    )
    alpha = next(item for item in snapshot.observations if item.provider_id == "fixture-alpha")
    assert snapshot.state is ChainState.UNKNOWN
    assert alpha.error_code == "stale_observation"


def test_provider_payload_total_cannot_exceed_maximum_supply():
    alpha = _json_bytes(
        {
            "utxos": [
                {
                    "confirmed": True,
                    "script_pubkey_hex": ADDRESS_SCRIPT,
                    "txid": TXID,
                    "value_sats": MAX_BITCOIN_SUPPLY_SATS,
                    "vout": 0,
                },
                {
                    "confirmed": False,
                    "script_pubkey_hex": ADDRESS_SCRIPT,
                    "txid": "22" * 32,
                    "value_sats": 1,
                    "vout": 1,
                },
            ]
        }
    )
    beta = _json_bytes(
        {
            "outputs": [
                {
                    "is_confirmed": True,
                    "locking_script": ADDRESS_SCRIPT,
                    "output_index": 0,
                    "satoshis": MAX_BITCOIN_SUPPLY_SATS,
                    "transaction_id": TXID,
                },
                {
                    "is_confirmed": False,
                    "locking_script": ADDRESS_SCRIPT,
                    "output_index": 1,
                    "satoshis": 1,
                    "transaction_id": "22" * 32,
                },
            ]
        }
    )
    snapshot = _collect(_Transport(_responses(alpha_utxos=alpha, beta_utxos=beta)))
    assert snapshot.state is ChainState.UNKNOWN
    assert {item.error_code for item in snapshot.observations} == {"utxos_invalid_payload"}


def test_registry_rejects_untyped_duplicate_or_empty_provider_selection():
    with pytest.raises(ChainAcquisitionError, match="registry factory"):
        ProviderRegistry(())  # type: ignore[call-arg]
    with pytest.raises(ChainAcquisitionError, match="typed"):
        ProviderRegistry.fixture(("fixture-alpha",))  # type: ignore[arg-type]
    with pytest.raises(ChainAcquisitionError, match="duplicate"):
        ProviderRegistry.fixture((FixtureProvider.ALPHA, FixtureProvider.ALPHA))
    with pytest.raises(ChainAcquisitionError, match="empty"):
        ProviderRegistry.fixture(())


def test_clock_must_be_a_trusted_aware_datetime_source():
    transport = _Transport(_responses())
    with pytest.raises(ChainAcquisitionError, match="aware datetime"):
        collect_chain_evidence(
            target=_target(),
            purpose=ChainPurpose.SELECTION,
            registry=ProviderRegistry.fixture(),
            transport=transport,
            clock=lambda: datetime(2026, 8, 20, 12, 0),
        )


def test_fixture_adapters_are_strict_and_do_not_carry_registry_provenance():
    alpha = FixtureAlphaAdapter()
    beta = FixtureBetaAdapter()
    assert alpha.parse_utxos(_alpha_utxos())[0].value_sats == 100_000
    assert beta.parse_utxos(_beta_utxos())[0].value_sats == 100_000
    assert alpha.parse_tip_height(_json_bytes({"tip_height": 900_000})) == 900_000
    assert beta.parse_tip_height(_json_bytes({"height": 900_000})) == 900_000
    with pytest.raises(ProviderPayloadError):
        alpha.parse_tip_height(_json_bytes({"tip_height": 900_000, "provider_id": "evil"}))
    assert not hasattr(alpha, "authority")
    assert not hasattr(beta, "independence_group")


def test_production_registry_verifies_original_transactions_before_quorum():
    transport = _ProductionTransport(_production_responses())
    snapshot = _collect_production(transport)

    assert snapshot.state is ChainState.FUNDED_CONFIRMED
    assert snapshot.confirmed_sats == 100_000
    assert snapshot.agreed_utxos[0].script_pubkey_hex == ADDRESS_SCRIPT
    assert {item.provider_id for item in snapshot.observations} == {
        "blockstream-info",
        "mempool-space",
    }
    assert {item.authority for item in snapshot.observations} == {ProviderAuthority.PUBLIC}
    assert {item.independence_group for item in snapshot.observations} == {
        "blockstream-info",
        "mempool-space",
    }
    assert transport.calls == [
        ("mempool-space", ProviderResource.UTXOS, ADDRESS, None),
        ("mempool-space", ProviderResource.TIP, ADDRESS, None),
        ("mempool-space", ProviderResource.TRANSACTION, ADDRESS, TXID),
        ("blockstream-info", ProviderResource.UTXOS, ADDRESS, None),
        ("blockstream-info", ProviderResource.TIP, ADDRESS, None),
        ("blockstream-info", ProviderResource.TRANSACTION, ADDRESS, TXID),
    ]


def test_production_original_transaction_must_match_txid_value_status_and_script():
    bad_transactions = (
        _esplora_transaction(txid="33" * 32),
        _esplora_transaction(value_sats=99_999),
        _esplora_transaction(confirmed=False),
        _esplora_transaction(script_pubkey_hex="51"),
    )
    expected_codes = (
        "transaction_invalid_payload",
        "transaction_invalid_payload",
        "transaction_invalid_payload",
        "utxo_address_mismatch",
    )
    for transaction, expected_code in zip(bad_transactions, expected_codes, strict=True):
        snapshot = _collect_production(
            _ProductionTransport(_production_responses(mempool_transaction=transaction))
        )
        mempool = next(
            item for item in snapshot.observations if item.provider_id == "mempool-space"
        )
        assert snapshot.state is ChainState.UNKNOWN
        assert mempool.outcome is ProviderOutcome.ERROR
        assert mempool.error_code == expected_code
        assert not mempool.utxos


def test_production_confirmation_metadata_must_match_transaction_and_tip():
    transaction = json.loads(_esplora_transaction())
    transaction["status"]["block_hash"] = "44" * 32
    snapshot = _collect_production(
        _ProductionTransport(_production_responses(mempool_transaction=_json_bytes(transaction)))
    )
    mempool = next(item for item in snapshot.observations if item.provider_id == "mempool-space")
    assert snapshot.state is ChainState.UNKNOWN
    assert mempool.error_code == "transaction_invalid_payload"

    snapshot = _collect_production(_ProductionTransport(_production_responses(mempool_tip=899_999)))
    mempool = next(item for item in snapshot.observations if item.provider_id == "mempool-space")
    assert snapshot.state is ChainState.UNKNOWN
    assert mempool.error_code == "utxos_invalid_payload"


def test_production_transaction_http_failure_and_two_source_disagreement_are_unknown():
    responses = _production_responses()
    responses[("mempool-space", ProviderResource.TRANSACTION, TXID)] = RawHttpResponse(
        404,
        b"not found",
    )
    snapshot = _collect_production(_ProductionTransport(responses))
    mempool = next(item for item in snapshot.observations if item.provider_id == "mempool-space")
    assert snapshot.state is ChainState.UNKNOWN
    assert snapshot.unknown_reason == "provider_error"
    assert mempool.error_code == "transaction_http_404"

    snapshot = _collect_production(
        _ProductionTransport(
            _production_responses(
                blockstream_utxos=_esplora_utxos(value_sats=99_999),
                blockstream_transaction=_esplora_transaction(value_sats=99_999),
            )
        )
    )
    assert snapshot.state is ChainState.UNKNOWN
    assert snapshot.unknown_reason == "provider_utxo_disagreement"


def test_esplora_adapter_rejects_remote_provenance_and_reuses_transaction_payload():
    adapter = EsploraAdapter()
    payload = _json_bytes(
        [
            {
                "status": _esplora_status(),
                "txid": TXID,
                "value": 1,
                "vout": 0,
            },
            {
                "status": _esplora_status(),
                "txid": TXID,
                "value": 2,
                "vout": 1,
            },
        ]
    )
    transaction = _json_bytes(
        {
            "status": _esplora_status(),
            "txid": TXID,
            "vout": [
                {"scriptpubkey": ADDRESS_SCRIPT, "value": 1},
                {"scriptpubkey": ADDRESS_SCRIPT, "value": 2},
            ],
        }
    )
    calls: list[str] = []

    def load_transaction(txid: str) -> bytes:
        calls.append(txid)
        return transaction

    assert [
        item.value_sats
        for item in adapter.collect_utxos(
            payload,
            tip_height=900_002,
            transaction_payload=load_transaction,
        )
    ] == [1, 2]
    assert calls == [TXID]

    spoofed = json.loads(_esplora_utxos())
    spoofed[0]["authority"] = "trusted_node"
    with pytest.raises(ProviderPayloadError):
        adapter.collect_utxos(
            _json_bytes(spoofed),
            tip_height=900_002,
            transaction_payload=load_transaction,
        )
    assert not hasattr(adapter, "authority")
    assert not hasattr(adapter, "independence_group")


def test_esplora_transaction_lookup_count_limit_is_unknown(monkeypatch):
    monkeypatch.setattr(chain_mod, "_MAX_TRANSACTION_LOOKUPS_PER_PROVIDER", 1)
    payload = _json_bytes(
        [
            {
                "status": _esplora_status(),
                "txid": TXID,
                "value": 1,
                "vout": 0,
            },
            {
                "status": _esplora_status(),
                "txid": "33" * 32,
                "value": 2,
                "vout": 1,
            },
        ]
    )
    snapshot = _collect_production(
        _ProductionTransport(
            _production_responses(
                mempool_utxos=payload,
                blockstream_utxos=payload,
            )
        )
    )
    assert snapshot.state is ChainState.UNKNOWN
    assert {item.error_code for item in snapshot.observations} == {"utxos_invalid_payload"}


def test_production_registry_is_typed_and_locally_fixed():
    registry = ProviderRegistry.production()
    assert registry.provider_ids == ("mempool-space", "blockstream-info")
    assert is_provider_registry_issued(registry)
    with pytest.raises(TypeError):
        ProviderRegistry.production((FixtureProvider.ALPHA,))  # type: ignore[call-arg]
    assert set(ProductionProvider) == {
        ProductionProvider.MEMPOOL_SPACE,
        ProductionProvider.BLOCKSTREAM_INFO,
    }


class _StreamingHttpResponse:
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        *,
        status_code: int = 200,
        content_length: str | None = None,
        before_chunk: Callable[[], None] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = {} if content_length is None else {"Content-Length": content_length}
        self.chunks = chunks
        self.closed = False
        self.chunk_sizes: list[int] = []
        self.before_chunk = before_chunk

    def iter_content(self, *, chunk_size: int):
        self.chunk_sizes.append(chunk_size)
        for chunk in self.chunks:
            if self.before_chunk is not None:
                self.before_chunk()
            yield chunk

    def close(self) -> None:
        self.closed = True


class _ProductionHttpSession:
    def __init__(
        self,
        responses: dict[tuple[str, ProviderResource, str | None], object],
        *,
        before_chunk: Callable[[], None] | None = None,
    ) -> None:
        self.responses = responses
        self.before_chunk = before_chunk
        self.trust_env = True
        self.closed = False
        self.calls: list[tuple[str, dict[str, object], bool]] = []
        self.http_responses: list[_StreamingHttpResponse] = []

    def get(self, url: str, **kwargs: object) -> _StreamingHttpResponse:
        self.calls.append((url, kwargs, self.trust_env))
        provider_id = (
            "mempool-space" if url.startswith("https://mempool.space/") else ("blockstream-info")
        )
        if "/address/" in url and url.endswith("/utxo"):
            key = (provider_id, ProviderResource.UTXOS, None)
        elif url.endswith("/blocks/tip/height"):
            key = (provider_id, ProviderResource.TIP, None)
        else:
            key = (provider_id, ProviderResource.TRANSACTION, url.rsplit("/", 1)[-1])
        raw = self.responses[key]
        if isinstance(raw, Exception):
            raise raw
        assert type(raw) is RawHttpResponse
        response = _StreamingHttpResponse(
            (raw.body,),
            status_code=raw.status_code,
            content_length=str(len(raw.body)),
            before_chunk=self.before_chunk,
        )
        self.http_responses.append(response)
        return response

    def close(self) -> None:
        self.closed = True


def _formal_production_responses(
    *,
    mempool_tip: int = 963_000,
    blockstream_tip: int = 963_002,
) -> dict[tuple[str, ProviderResource, str | None], object]:
    return _production_responses(
        mempool_tip=mempool_tip,
        blockstream_tip=blockstream_tip,
    )


def _patch_production_runtime(monkeypatch, session: _ProductionHttpSession) -> None:
    monkeypatch.setattr(chain_mod, "_new_requests_session", lambda: session)
    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)
    monkeypatch.setattr(chain_mod, "_monotonic_seconds", lambda: 0.0)


def test_sealed_production_collector_owns_session_clock_budget_and_provenance(monkeypatch):
    session = _ProductionHttpSession(_formal_production_responses())
    _patch_production_runtime(monkeypatch, session)
    monkeypatch.setattr(
        chain_mod.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("production must use its explicit Session")
        ),
    )

    evidence = collect_production_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
    )

    assert type(evidence) is ChainAdmissionReceipt
    assert evidence.provenance is ChainEvidenceProvenance.PRODUCTION_HTTP_V1
    assert is_chain_admission_receipt_issued(evidence)
    assert is_production_chain_admission_receipt_issued(evidence)
    assert evidence.snapshot.state is ChainState.FUNDED_CONFIRMED
    assert session.closed
    assert len(session.calls) == 6
    assert all(trust_env is False for _, _, trust_env in session.calls)
    assert all(response.closed for response in session.http_responses)
    for _, kwargs, _ in session.calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        assert kwargs["headers"]["Accept-Encoding"] == "identity"  # type: ignore[index]
        connect_timeout, read_timeout = kwargs["timeout"]  # type: ignore[misc]
        assert 0 < connect_timeout <= 3.05
        assert 0 < read_timeout <= 10.0

    with pytest.raises(TypeError):
        collect_production_chain_evidence(  # type: ignore[call-arg]
            target=_target(),
            purpose=ChainPurpose.SELECTION,
            clock=lambda: NOW,
        )


def test_injected_production_schema_cannot_masquerade_as_production_receipt():
    receipt = collect_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
        registry=ProviderRegistry.production(),
        transport=_ProductionTransport(_production_responses()),
        clock=_Clock((NOW, NOW, NOW)),
    )
    assert type(receipt) is ChainAdmissionReceipt
    assert receipt.provenance is ChainEvidenceProvenance.INJECTED_V1
    assert is_chain_admission_receipt_issued(receipt)
    assert not is_production_chain_admission_receipt_issued(receipt)

    object.__setattr__(
        receipt,
        "provenance",
        ChainEvidenceProvenance.PRODUCTION_HTTP_V1,
    )
    assert not is_chain_admission_receipt_issued(receipt)
    assert not is_production_chain_admission_receipt_issued(receipt)


def test_production_practice_bypass_creates_no_session_or_production_receipt(monkeypatch):
    entry = _practice_entry()
    assert entry.practice_fixture is not None
    monkeypatch.setattr(
        chain_mod,
        "_new_requests_session",
        lambda: (_ for _ in ()).throw(AssertionError("practice must create no HTTP session")),
    )
    monkeypatch.setattr(
        chain_mod,
        "_monotonic_seconds",
        lambda: (_ for _ in ()).throw(AssertionError("practice must consume no HTTP budget")),
    )
    evidence = collect_production_chain_evidence(
        target=entry.target,
        purpose=ChainPurpose.SELECTION,
        practice_fixture=entry.practice_fixture,
    )
    assert type(evidence) is PracticeLookupBypass
    assert evidence.provenance is ChainEvidenceProvenance.CATALOG_PRACTICE_V1
    assert is_practice_lookup_bypass_issued(evidence)
    assert not is_production_chain_admission_receipt_issued(evidence)


def test_production_checkpoint_rejects_ancient_tips_without_claiming_freshness(monkeypatch):
    session = _ProductionHttpSession(
        _formal_production_responses(mempool_tip=962_999, blockstream_tip=962_999)
    )
    _patch_production_runtime(monkeypatch, session)
    evidence = collect_production_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
    )
    assert type(evidence) is ChainAdmissionReceipt
    assert evidence.provenance is ChainEvidenceProvenance.PRODUCTION_HTTP_V1
    assert evidence.snapshot.state is ChainState.UNKNOWN
    assert evidence.snapshot.unknown_reason == "provider_error"
    assert {item.error_code for item in evidence.snapshot.observations} == {
        "tip_below_mainnet_checkpoint_v1"
    }
    assert session.closed


def test_production_total_deadline_stops_slow_stream_and_closes_session(monkeypatch):
    monotonic = [0.0]

    def expire_during_chunk() -> None:
        monotonic[0] = 61.0

    session = _ProductionHttpSession(
        _formal_production_responses(),
        before_chunk=expire_during_chunk,
    )
    monkeypatch.setattr(chain_mod, "_new_requests_session", lambda: session)
    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)
    monkeypatch.setattr(chain_mod, "_monotonic_seconds", lambda: monotonic[0])
    evidence = collect_production_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
    )
    assert type(evidence) is ChainAdmissionReceipt
    assert evidence.snapshot.state is ChainState.UNKNOWN
    assert {item.error_code for item in evidence.snapshot.observations} == {"utxos_transport_error"}
    assert len(session.calls) == 1
    assert session.closed


def test_production_request_and_cumulative_byte_budgets_fail_closed(monkeypatch):
    request_limited = _ProductionHttpSession(_formal_production_responses())
    _patch_production_runtime(monkeypatch, request_limited)
    monkeypatch.setattr(chain_mod, "_PRODUCTION_HTTP_REQUEST_LIMIT", 1)
    evidence = collect_production_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
    )
    assert type(evidence) is ChainAdmissionReceipt
    assert evidence.snapshot.state is ChainState.UNKNOWN
    assert len(request_limited.calls) == 1
    assert request_limited.closed

    byte_responses = _formal_production_responses()
    byte_limited = _ProductionHttpSession(byte_responses)
    monkeypatch.setattr(chain_mod, "_new_requests_session", lambda: byte_limited)
    monkeypatch.setattr(chain_mod, "_PRODUCTION_HTTP_REQUEST_LIMIT", 256)
    first_utxo = byte_responses[("mempool-space", ProviderResource.UTXOS, None)]
    first_tip = byte_responses[("mempool-space", ProviderResource.TIP, None)]
    assert type(first_utxo) is RawHttpResponse
    assert type(first_tip) is RawHttpResponse
    monkeypatch.setattr(
        chain_mod,
        "_PRODUCTION_HTTP_TOTAL_BYTES_LIMIT",
        len(first_utxo.body) + len(first_tip.body) + 1,
    )
    evidence = collect_production_chain_evidence(
        target=_target(),
        purpose=ChainPurpose.SELECTION,
    )
    assert type(evidence) is ChainAdmissionReceipt
    assert evidence.snapshot.state is ChainState.UNKNOWN
    assert len(byte_limited.calls) == 4
    assert byte_limited.closed


def test_http_transport_uses_only_fixed_read_endpoints_and_injected_requests():
    calls: list[tuple[str, dict[str, object]]] = []
    responses: list[_StreamingHttpResponse] = []

    def request_get(url: str, **kwargs: object) -> _StreamingHttpResponse:
        calls.append((url, kwargs))
        response = _StreamingHttpResponse((b"first", b"", b"second"))
        responses.append(response)
        return response

    transport = HttpChainTransport(request_get=request_get)
    assert (
        transport.get(
            provider_id="mempool-space",
            resource=ProviderResource.UTXOS,
            address=ADDRESS,
        ).body
        == b"firstsecond"
    )
    transport.get(
        provider_id="blockstream-info",
        resource=ProviderResource.TIP,
        address=ADDRESS,
    )
    transport.get(
        provider_id="mempool-space",
        resource=ProviderResource.TRANSACTION,
        address=ADDRESS,
        txid=TXID,
    )

    assert [call[0] for call in calls] == [
        f"https://mempool.space/api/address/{ADDRESS}/utxo",
        "https://blockstream.info/api/blocks/tip/height",
        f"https://mempool.space/api/tx/{TXID}",
    ]
    for _, kwargs in calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        assert kwargs["timeout"] == (3.05, 10.0)
        assert "User-Agent" in kwargs["headers"]  # type: ignore[operator]
    assert all(response.closed for response in responses)
    assert all(response.chunk_sizes == [1] for response in responses)


def test_http_transport_bounds_streams_and_normalizes_request_failures():
    oversized = _StreamingHttpResponse((b"123", b"456"))
    transport = HttpChainTransport(
        request_get=lambda *_args, **_kwargs: oversized, max_response_bytes=5
    )
    with pytest.raises(HttpTransportError, match="byte limit"):
        transport.get(
            provider_id="mempool-space",
            resource=ProviderResource.TIP,
            address=ADDRESS,
        )
    assert oversized.closed

    announced = _StreamingHttpResponse((b"123",), content_length="6")
    transport = HttpChainTransport(
        request_get=lambda *_args, **_kwargs: announced, max_response_bytes=5
    )
    with pytest.raises(HttpTransportError, match="byte limit"):
        transport.get(
            provider_id="mempool-space",
            resource=ProviderResource.TIP,
            address=ADDRESS,
        )
    assert announced.closed

    def fail(*_args, **_kwargs):
        raise TimeoutError("network timeout")

    with pytest.raises(HttpTransportError, match="GET failed"):
        HttpChainTransport(request_get=fail).get(
            provider_id="mempool-space",
            resource=ProviderResource.TIP,
            address=ADDRESS,
        )


@pytest.mark.parametrize("timeout", [0, -1.0, (1.0, 0.0), True, "10"])
def test_http_transport_rejects_unbounded_or_ambiguous_configuration(timeout):
    with pytest.raises(HttpTransportError):
        HttpChainTransport(timeout_seconds=timeout)  # type: ignore[arg-type]
