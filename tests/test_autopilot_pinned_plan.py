import json
from datetime import UTC, datetime

import pytest

from btc_puzzle_lab.autopilot.catalog_view import snapshot_from_puzzles
from btc_puzzle_lab.autopilot.chain import (
    ChainEvidenceProvenance,
    FixtureProvider,
    ProviderRegistry,
    ProviderResource,
    RawHttpResponse,
    collect_chain_evidence,
)
from btc_puzzle_lab.autopilot.facts import ChainPurpose, GpuDevice, HostCapabilities
from btc_puzzle_lab.autopilot.host import HostDiscoveryCode, HostDiscoveryError
from btc_puzzle_lab.autopilot.pinned_plan import (
    PinnedPlanError,
    PinnedPlanErrorCode,
    PinnedPlanOutcome,
    PinnedPlanPorts,
    PinnedPlanStage,
    build_pinned_plan,
    production_pinned_plan_ports,
)
from btc_puzzle_lab.catalog import Puzzle

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
ADDRESS = "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
ADDRESS_SCRIPT = "76a914751e76e8199196d454941c45d1b3a323f1433bd688ac"
PUBKEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
TXID = "11" * 32
GIB = 1024**3


def _live_puzzle() -> Puzzle:
    return Puzzle(
        id=71,
        bits=7,
        address=ADDRESS,
        range_start=1,
        range_end=100,
        pubkey_compressed_hex="",
        practice_solution=None,
        status="unsolved",
        engine_default="auto",
        notes="pinned plan live target",
    )


def _practice_puzzle() -> Puzzle:
    return Puzzle(
        id=1,
        bits=1,
        address=ADDRESS,
        range_start=1,
        range_end=1,
        pubkey_compressed_hex=PUBKEY,
        practice_solution=1,
        status="solved",
        engine_default="sequential",
        notes="public practice fixture",
    )


def _host() -> HostCapabilities:
    return HostCapabilities(
        architecture="x86_64",
        cpu_count=8,
        memory_bytes=16 * GIB,
        disk_free_bytes=100 * GIB,
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class _Transport:
    def __init__(self, state: str) -> None:
        self.state = state
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
        if self.state == "unknown":
            return RawHttpResponse(503, b"unavailable")
        if resource is ProviderResource.TIP:
            key = "tip_height" if provider_id == FixtureProvider.ALPHA else "height"
            return RawHttpResponse(200, _json_bytes({key: 900_000}))
        if resource is ProviderResource.TRANSACTION:
            raise AssertionError("fixture evidence must not fetch transactions")
        if self.state == "empty":
            key = "utxos" if provider_id == FixtureProvider.ALPHA else "outputs"
            return RawHttpResponse(200, _json_bytes({key: []}))
        if provider_id == FixtureProvider.ALPHA:
            payload = {
                "utxos": [
                    {
                        "txid": TXID,
                        "vout": 0,
                        "value_sats": 100_000,
                        "script_pubkey_hex": ADDRESS_SCRIPT,
                        "confirmed": True,
                    }
                ]
            }
        else:
            payload = {
                "outputs": [
                    {
                        "transaction_id": TXID,
                        "output_index": 0,
                        "satoshis": 100_000,
                        "locking_script": ADDRESS_SCRIPT,
                        "is_confirmed": True,
                    }
                ]
            }
        return RawHttpResponse(200, _json_bytes(payload))


class _InjectedEsploraTransport:
    def get(
        self,
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
        txid: str | None = None,
    ) -> RawHttpResponse:
        del provider_id, address
        status = {
            "block_hash": "22" * 32,
            "block_height": 900_000,
            "block_time": 1_700_000_000,
            "confirmed": True,
        }
        if resource is ProviderResource.TIP:
            return RawHttpResponse(200, b"900000\n")
        if resource is ProviderResource.UTXOS:
            return RawHttpResponse(
                200,
                _json_bytes([{"status": status, "txid": TXID, "value": 100_000, "vout": 0}]),
            )
        assert txid == TXID
        return RawHttpResponse(
            200,
            _json_bytes(
                {
                    "status": status,
                    "txid": TXID,
                    "vout": [{"scriptpubkey": ADDRESS_SCRIPT, "value": 100_000}],
                }
            ),
        )


def _ports(
    puzzle: Puzzle,
    *,
    state: str,
    events: list[str] | None = None,
) -> tuple[PinnedPlanPorts, _Transport]:
    snapshot = snapshot_from_puzzles((puzzle,))
    transport = _Transport(state)
    observed = events if events is not None else []

    def load_catalog():
        observed.append("catalog")
        return snapshot

    def bind_target(loaded, puzzle_id):
        observed.append("binding")
        assert loaded is snapshot
        return loaded.bind_target(puzzle_id)

    def discover_host():
        observed.append("host")
        return _host()

    def clock():
        observed.append("clock")
        return NOW

    def collect_chain(*, binding, purpose, clock):
        observed.append("chain")
        assert purpose is ChainPurpose.SELECTION
        return collect_chain_evidence(
            target=binding.target,
            purpose=purpose,
            registry=ProviderRegistry.fixture(),
            transport=transport,
            clock=clock,
            practice_fixture=binding.practice_fixture,
        )

    return (
        PinnedPlanPorts(
            load_catalog=load_catalog,
            bind_target=bind_target,
            discover_host=discover_host,
            collect_chain=collect_chain,
            clock=clock,
        ),
        transport,
    )


def _blocker_codes(report) -> set[str]:
    return {blocker.code for blocker in report.target_blockers}


def test_live_funded_flow_is_strict_deterministic_and_detached_from_sensitive_material():
    events: list[str] = []
    ports, _transport = _ports(_live_puzzle(), state="funded", events=events)

    report = build_pinned_plan(71, ports=ports)

    assert events[0:4] == ["catalog", "binding", "host", "chain"]
    assert events[-1] == "clock"
    assert report.outcome is PinnedPlanOutcome.SELECTED
    assert report.selected
    assert report.selected_engine == "sequential"
    assert len(report.selection_fingerprint or "") == 64
    assert report.target.mode == "live"
    assert report.chain.state == "FUNDED_CONFIRMED"
    assert report.chain.provenance == ChainEvidenceProvenance.FIXTURE_V1.value
    assert report.chain.confirmed_sats == 100_000
    assert report.chain.unconfirmed_sats == 0
    assert len(report.algorithms) == 5
    assert {algorithm.engine for algorithm in report.algorithms} == {
        "sequential",
        "keyhunt",
        "kangaroo",
        "rckangaroo",
        "bitcrack",
    }
    for algorithm in report.algorithms:
        assert algorithm.provisioning in {"built_in", "auto_build", "manual_required"}
        assert isinstance(algorithm.blockers, tuple)
        if algorithm.estimate is not None:
            assert algorithm.estimate.source == "baseline"
            assert algorithm.estimate.confidence.denominator > 0

    text = report.render_text()
    assert text == report.render_text()
    assert report.selection_fingerprint in text
    assert "expected_full_eta=" in text
    assert "horizon_hit_probability=" in text
    assert "memory_floor:" in text
    assert "Preparation=not_run" in text
    assert "not measured on this host" in text
    assert f"checked_at={NOW.isoformat().replace('+00:00', 'Z')}" in text
    assert "fresh_until=" in text
    assert 15 <= len(text.splitlines()) <= 30
    for forbidden in (ADDRESS, PUBKEY, TXID, "signed_tx", "private_key_hex"):
        assert forbidden not in text


@pytest.mark.parametrize(
    ("state", "chain_state", "blocker"),
    [
        ("empty", "EMPTY", "PRIZE_SWEPT"),
        ("unknown", "UNKNOWN", "PRIZE_UNKNOWN"),
    ],
)
def test_live_non_funded_states_return_complete_blocked_reports(
    state: str,
    chain_state: str,
    blocker: str,
):
    ports, _transport = _ports(_live_puzzle(), state=state)

    report = build_pinned_plan(71, ports=ports)

    assert report.outcome is PinnedPlanOutcome.BLOCKED
    assert not report.selected
    assert report.selected_engine is None
    assert report.selection_fingerprint is None
    assert report.chain.state == chain_state
    assert blocker in _blocker_codes(report)
    assert len(report.algorithms) == 5
    assert all(algorithm.explanation for algorithm in report.algorithms)
    assert f"[{blocker}]" in report.render_text()
    if state == "unknown":
        assert "unknown_reason=provider_error" in report.render_text()


def test_practice_flow_uses_catalog_bypass_without_chain_transport():
    ports, transport = _ports(_practice_puzzle(), state="funded")

    report = build_pinned_plan(1, ports=ports)

    assert report.outcome is PinnedPlanOutcome.SELECTED
    assert report.target.mode == "practice"
    assert report.chain.evidence_kind == "practice_bypass"
    assert report.chain.provenance == ChainEvidenceProvenance.CATALOG_PRACTICE_V1.value
    assert report.chain.state == "PRACTICE"
    assert report.chain.confirmed_sats is None
    assert report.chain.unconfirmed_sats is None
    assert transport.calls == []
    text = report.render_text()
    assert PUBKEY not in text
    assert "practice_solution" not in text


def test_text_report_lists_safe_physical_gpu_topology():
    ports, _transport = _ports(_live_puzzle(), state="funded")
    gpu_host = HostCapabilities(
        architecture="x86_64",
        cpu_count=16,
        memory_bytes=64 * GIB,
        disk_free_bytes=100 * GIB,
        gpus=(
            GpuDevice(
                device_id="GPU-safe-id",
                name="NVIDIA test GPU",
                memory_bytes=24 * GIB,
                compute_capability=(8, 9),
                multiprocessor_count=128,
            ),
        ),
    )
    ports = PinnedPlanPorts(
        load_catalog=ports.load_catalog,
        bind_target=ports.bind_target,
        discover_host=lambda: gpu_host,
        collect_chain=ports.collect_chain,
        clock=ports.clock,
    )

    text = build_pinned_plan(71, ports=ports).render_text()

    assert (
        'host gpu: id=GPU-safe-id name="NVIDIA test GPU" '
        "memory=25769803776B(~24.0GiB) compute_capability=8.9 multiprocessors=128"
    ) in text


def test_dependency_failure_is_typed_actionable_and_does_not_become_a_blocked_report():
    ports, _transport = _ports(_live_puzzle(), state="funded")

    def broken_host():
        raise OSError("machine-specific path is intentionally not exposed")

    ports = PinnedPlanPorts(
        load_catalog=ports.load_catalog,
        bind_target=ports.bind_target,
        discover_host=broken_host,
        collect_chain=ports.collect_chain,
        clock=ports.clock,
    )

    with pytest.raises(PinnedPlanError) as captured:
        build_pinned_plan(71, ports=ports)

    error = captured.value
    assert error.stage is PinnedPlanStage.HOST_DISCOVERY
    assert error.code is PinnedPlanErrorCode.HOST_DISCOVERY_FAILED
    assert error.detail == "physical host discovery failed (OSError)"
    assert error.remedy == "repair access to physical CPU, memory, disk, and GPU facts"
    assert "machine-specific" not in error.detail


def test_nvidia_probe_failure_uses_static_mig_remedy_without_leaking_cause_text():
    ports, _transport = _ports(_live_puzzle(), state="funded")
    sentinel = "SENTINEL_MACHINE_DETAIL_MUST_NOT_ESCAPE"

    def broken_gpu_discovery():
        raise HostDiscoveryError(HostDiscoveryCode.NVIDIA_PROBE_FAILED, sentinel)

    ports = PinnedPlanPorts(
        load_catalog=ports.load_catalog,
        bind_target=ports.bind_target,
        discover_host=broken_gpu_discovery,
        collect_chain=ports.collect_chain,
        clock=ports.clock,
    )

    with pytest.raises(PinnedPlanError) as captured:
        build_pinned_plan(71, ports=ports)

    error = captured.value
    assert error.stage is PinnedPlanStage.HOST_DISCOVERY
    assert error.code is PinnedPlanErrorCode.HOST_DISCOVERY_FAILED
    assert error.detail == "physical host discovery failed (nvidia_probe_failed)"
    assert "Preparation" in error.remedy
    assert "MIG-aware" in error.remedy
    assert sentinel not in str(error)


def test_production_port_factory_assembles_adapters_without_starting_io(monkeypatch):
    calls: list[object] = []

    def forbidden_get(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("port construction must not start network I/O")

    monkeypatch.setattr("btc_puzzle_lab.autopilot.chain.requests.get", forbidden_get)
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.chain._new_requests_session",
        lambda: (_ for _ in ()).throw(AssertionError("factory must not create a session")),
    )

    ports = production_pinned_plan_ports()

    assert type(ports) is PinnedPlanPorts
    assert callable(ports.load_catalog)
    assert callable(ports.bind_target)
    assert callable(ports.discover_host)
    assert callable(ports.collect_chain)
    assert callable(ports.clock)
    assert ports.require_production_chain_receipt
    assert calls == []


def test_production_pinned_boundary_rejects_injected_live_receipt():
    base_ports, _transport = _ports(_live_puzzle(), state="funded")

    def collect_injected(*, binding, purpose, clock):
        receipt = collect_chain_evidence(
            target=binding.target,
            purpose=purpose,
            registry=ProviderRegistry.production(),
            transport=_InjectedEsploraTransport(),
            clock=clock,
        )
        assert receipt.provenance is ChainEvidenceProvenance.INJECTED_V1
        return receipt

    ports = PinnedPlanPorts(
        load_catalog=base_ports.load_catalog,
        bind_target=base_ports.bind_target,
        discover_host=base_ports.discover_host,
        collect_chain=collect_injected,
        clock=base_ports.clock,
        require_production_chain_receipt=True,
    )

    with pytest.raises(PinnedPlanError) as captured:
        build_pinned_plan(71, ports=ports)
    assert captured.value.code is PinnedPlanErrorCode.PORT_CONTRACT_VIOLATION
    assert captured.value.stage is PinnedPlanStage.CHAIN_COLLECTION
