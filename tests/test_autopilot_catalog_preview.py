from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest

import btc_puzzle_lab.autopilot.catalog_preview as preview_mod
import btc_puzzle_lab.autopilot.chain as chain_mod
from btc_puzzle_lab.autopilot.catalog_preview import (
    CatalogPreviewError,
    CatalogPreviewErrorCode,
    CatalogPreviewOutcome,
    CatalogPreviewPorts,
    CatalogPreviewStage,
    build_catalog_preview,
    production_catalog_preview_ports,
)
from btc_puzzle_lab.autopilot.catalog_ranking import (
    CatalogFastestRankingReceipt,
    rank_catalog_fastest,
)
from btc_puzzle_lab.autopilot.catalog_view import load_snapshot, snapshot_from_puzzles
from btc_puzzle_lab.autopilot.chain import (
    CatalogChainBatchReceipt,
    ChainEvidenceProvenance,
    collect_production_catalog_prefix,
)
from btc_puzzle_lab.autopilot.facts import HostCapabilities
from btc_puzzle_lab.autopilot.host import discover_host
from btc_puzzle_lab.catalog import load_packaged_full_puzzles
from btc_puzzle_lab.crypto import address_hash160

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
GIB = 1024**3


def _host(*, cpus: int = 8) -> HostCapabilities:
    return HostCapabilities(
        architecture="x86_64",
        cpu_count=cpus,
        memory_bytes=16 * GIB,
        disk_free_bytes=100 * GIB,
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _script(address: str) -> str:
    return (b"\x76\xa9\x14" + address_hash160(address) + b"\x88\xac").hex()


def _status(*, confirmed: bool) -> dict[str, object]:
    if not confirmed:
        return {"confirmed": False}
    return {
        "block_hash": "22" * 32,
        "block_height": 963_000,
        "block_time": 1_700_000_000,
        "confirmed": True,
    }


class _WirePlan:
    def __init__(self, ranking: CatalogFastestRankingReceipt, states: tuple[str, ...]) -> None:
        self.claims: dict[str, dict[str, object]] = {}
        self.transactions: dict[str, bytes] = {}
        for index, candidate in enumerate(ranking.algorithmically_selectable):
            address = candidate.binding.target.address
            state = states[index] if index < len(states) else "empty"
            claim: dict[str, object] = {"state": state}
            if state in {"confirmed", "unconfirmed"}:
                txid = hashlib.sha256(f"preview-transaction-{index}".encode()).hexdigest()
                confirmed = state == "confirmed"
                claim.update({"txid": txid, "confirmed": confirmed})
                self.transactions[txid] = _json_bytes(
                    {
                        "status": _status(confirmed=confirmed),
                        "txid": txid,
                        "vout": [{"scriptpubkey": _script(address), "value": 1_000}],
                    }
                )
            self.claims[address] = claim


class _StreamingResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(body))}
        self.body = body
        self.closed = False

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 1
        yield self.body

    def close(self) -> None:
        self.closed = True


class _BatchSession:
    def __init__(self, provider_id: str, wire: _WirePlan) -> None:
        self.provider_id = provider_id
        self.wire = wire
        self.trust_env = True
        self.closed = False
        self.calls: list[str] = []

    def get(self, url: str, **kwargs: object) -> _StreamingResponse:
        origin = {
            "mempool-space": "https://mempool.space/api/",
            "blockstream-info": "https://blockstream.info/api/",
        }[self.provider_id]
        assert url.startswith(origin)
        assert self.trust_env is False
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        if "/address/" in url:
            address = unquote(url.split("/address/", 1)[1].removesuffix("/utxo"))
            claim = self.wire.claims[address]
            self.calls.append("utxos")
            if claim["state"] == "unknown":
                return _StreamingResponse(503, b"unavailable")
            if claim["state"] == "empty":
                return _StreamingResponse(200, b"[]")
            confirmed = bool(claim["confirmed"])
            return _StreamingResponse(
                200,
                _json_bytes(
                    [
                        {
                            "status": _status(confirmed=confirmed),
                            "txid": claim["txid"],
                            "value": 1_000,
                            "vout": 0,
                        }
                    ]
                ),
            )
        if url.endswith("/blocks/tip/height"):
            self.calls.append("tip")
            tip = 963_000 if self.provider_id == "mempool-space" else 963_001
            return _StreamingResponse(200, f"{tip}\n".encode())
        txid = url.rsplit("/", 1)[-1]
        self.calls.append("transaction")
        return _StreamingResponse(200, self.wire.transactions[txid])

    def close(self) -> None:
        self.closed = True


def _patch_chain_runtime(
    monkeypatch: pytest.MonkeyPatch,
    ranking: CatalogFastestRankingReceipt,
    states: tuple[str, ...],
) -> tuple[_BatchSession, _BatchSession]:
    wire = _WirePlan(ranking, states)
    sessions = (
        _BatchSession("mempool-space", wire),
        _BatchSession("blockstream-info", wire),
    )
    pending = iter(sessions)
    monkeypatch.setattr(chain_mod, "_new_requests_session", lambda: next(pending))
    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)
    monkeypatch.setattr(chain_mod, "_monotonic_seconds", lambda: 0.0)
    return sessions


def _ports(
    monkeypatch: pytest.MonkeyPatch,
    *,
    states: tuple[str, ...],
    host: HostCapabilities | None = None,
    events: list[str] | None = None,
    captured: dict[str, object] | None = None,
) -> CatalogPreviewPorts:
    chosen_host = host or _host()
    observed = events if events is not None else []
    values = captured if captured is not None else {}

    def load_catalog():
        observed.append("catalog")
        return load_snapshot()

    def discover():
        observed.append("host")
        return chosen_host

    def rank(snapshot, discovered, policy):
        observed.append("ranking")
        ranking = rank_catalog_fastest(snapshot, discovered, policy)
        values["ranking"] = ranking
        return ranking

    def collect(ranking):
        observed.append("chain")
        sessions = _patch_chain_runtime(monkeypatch, ranking, states)
        values["sessions"] = sessions
        batch = collect_production_catalog_prefix(ranking)
        values["batch"] = batch
        return batch

    def clock():
        observed.append("clock")
        return NOW

    return CatalogPreviewPorts(
        load_catalog=load_catalog,
        discover_host=discover,
        rank_catalog=rank,
        collect_prefix=collect,
        clock=clock,
    )


def test_selected_preview_runs_fixed_order_and_returns_detached_compact_report(monkeypatch):
    events: list[str] = []
    captured: dict[str, object] = {}
    report = build_catalog_preview(
        ports=_ports(
            monkeypatch,
            states=("empty", "unconfirmed", "confirmed"),
            events=events,
            captured=captured,
        )
    )

    assert events == ["catalog", "host", "ranking", "chain", "clock"]
    assert report.outcome is CatalogPreviewOutcome.SELECTED
    assert report.scope.total_count == 160
    assert report.scope.live_count == 78
    assert report.scope.practice_count == 82
    assert report.comparison.objective == "fastest_full_solution_eta_baseline_v1"
    assert report.comparison.confidence == "low"
    assert report.comparison.economic_optimum == "not_claimed"
    assert report.comparison.balanced_optimum == "not_claimed"
    assert report.ranking.candidate_count == 78
    assert report.ranking.algorithmically_selectable_count == 78
    assert report.ranking.statically_blocked_count == 0
    assert report.batch.checked_count == 3
    assert report.batch.not_checked_count == 75
    assert report.batch.request_count > 0
    assert report.batch.decompressed_bytes > 0
    assert report.batch.provenance == ChainEvidenceProvenance.PRODUCTION_CATALOG_HTTP_V1.value
    assert report.batch.started_at == "2026-08-21T12:00:00Z"
    assert report.batch.completed_at == "2026-08-21T12:00:00Z"
    assert report.batch.prefix_min_fresh_until == "2026-08-21T12:05:00Z"
    assert tuple(candidate.status for candidate in report.prefix.checked) == (
        "empty",
        "funded_unconfirmed",
        "funded_confirmed",
    )
    assert report.selected is not None
    assert report.selected.puzzle_id == report.prefix.checked[-1].puzzle_id
    assert report.selected.catalog_rank == 3
    assert report.selected.engine == report.prefix.checked[-1].engine
    assert report.selected.confirmed_sats == 1_000
    assert report.preparation == "not_run"
    assert report.execution_feasibility == "not_evaluated"
    assert report.prefix.terminal_rank == 3
    assert report.prefix.terminal_engine == report.selected.engine
    assert report.prefix.stop_reason == "confirmed_funded_candidate"
    assert report.authority == "detached"
    assert len(report.comparison.policy_fingerprint) == 64
    assert len(report.ranking.ranking_fingerprint) == 64
    assert len(report.batch.batch_fingerprint) == 64
    text = report.render_text()
    assert text == report.render_text()
    assert "economic/balanced optimum: not claimed" in text
    assert len(text.splitlines()) == 12
    assert not hasattr(report, "decision")
    assert not hasattr(report, "receipt")
    assert not hasattr(report, "ranking_receipt")

    sessions = captured["sessions"]
    assert all(session.closed for session in sessions)


def test_unknown_prefix_is_indeterminate_and_never_reports_a_selection(monkeypatch):
    report = build_catalog_preview(
        ports=_ports(monkeypatch, states=("empty", "unknown", "confirmed"))
    )

    assert report.outcome is CatalogPreviewOutcome.INDETERMINATE
    assert report.batch.checked_count == 2
    assert report.batch.not_checked_count == 76
    assert report.prefix.terminal_status == "unknown"
    assert report.selected is None
    assert "selected none" in report.render_text()


def test_no_algorithm_candidate_returns_no_feasible_without_network(monkeypatch):
    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)
    monkeypatch.setattr(
        chain_mod,
        "_new_requests_session",
        lambda: (_ for _ in ()).throw(AssertionError("no HTTP session expected")),
    )
    ports = CatalogPreviewPorts(
        load_catalog=load_snapshot,
        discover_host=lambda: _host(cpus=1),
        rank_catalog=rank_catalog_fastest,
        collect_prefix=collect_production_catalog_prefix,
        clock=lambda: NOW,
    )

    report = build_catalog_preview(ports=ports)

    assert report.outcome is CatalogPreviewOutcome.NO_CONFIRMED_SELECTABLE_TARGET
    assert report.ranking.algorithmically_selectable_count == 0
    assert report.ranking.statically_blocked_count == 78
    assert report.batch.checked_count == report.batch.not_checked_count == 0
    assert report.batch.request_count == report.batch.decompressed_bytes == 0
    assert report.batch.prefix_min_fresh_until is None
    assert report.prefix.checked == ()
    assert report.prefix.stop_reason == "no_algorithmically_selectable_candidates"
    assert report.prefix.terminal_puzzle_id is None
    assert report.selected is None


def test_exhausted_chain_prefix_reports_no_confirmed_selection_not_execution_feasibility(
    monkeypatch,
):
    report = build_catalog_preview(ports=_ports(monkeypatch, states=()))

    assert report.outcome is CatalogPreviewOutcome.NO_CONFIRMED_SELECTABLE_TARGET
    assert report.batch.checked_count == 78
    assert report.batch.not_checked_count == 0
    assert report.prefix.stop_reason == "ranked_candidates_exhausted"
    assert report.prefix.terminal_puzzle_id is None
    assert report.selected is None
    assert report.execution_feasibility == "not_evaluated"


def test_expired_issued_prefix_is_rejected_at_report_time(monkeypatch):
    base = _ports(monkeypatch, states=("confirmed",))
    ports = CatalogPreviewPorts(
        load_catalog=base.load_catalog,
        discover_host=base.discover_host,
        rank_catalog=base.rank_catalog,
        collect_prefix=base.collect_prefix,
        clock=lambda: NOW + timedelta(seconds=301),
    )

    with pytest.raises(CatalogPreviewError) as captured:
        build_catalog_preview(ports=ports)

    assert captured.value.stage is CatalogPreviewStage.REPORT
    assert captured.value.code is CatalogPreviewErrorCode.REPORT_CONTRACT_VIOLATION
    assert "expired" in captured.value.detail


def test_forged_ranking_and_batch_from_ports_are_rejected_before_reporting(monkeypatch):
    collector_called = False

    def forged_rank(snapshot, host, policy):
        issued = rank_catalog_fastest(snapshot, host, policy)
        forged = object.__new__(CatalogFastestRankingReceipt)
        for item in fields(CatalogFastestRankingReceipt):
            object.__setattr__(forged, item.name, getattr(issued, item.name))
        return forged

    def forbidden_collector(_ranking):
        nonlocal collector_called
        collector_called = True
        raise AssertionError("forged ranking reached chain collection")

    ranking_ports = CatalogPreviewPorts(
        load_catalog=load_snapshot,
        discover_host=_host,
        rank_catalog=forged_rank,
        collect_prefix=forbidden_collector,
        clock=lambda: NOW,
    )
    with pytest.raises(CatalogPreviewError) as ranking_error:
        build_catalog_preview(ports=ranking_ports)
    assert ranking_error.value.stage is CatalogPreviewStage.RANKING
    assert ranking_error.value.code is CatalogPreviewErrorCode.RANKING_AUTHORITY_INVALID
    assert not collector_called

    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)

    def forged_batch(ranking):
        issued = collect_production_catalog_prefix(ranking)
        forged = object.__new__(CatalogChainBatchReceipt)
        for item in fields(CatalogChainBatchReceipt):
            object.__setattr__(forged, item.name, getattr(issued, item.name))
        return forged

    batch_ports = CatalogPreviewPorts(
        load_catalog=load_snapshot,
        discover_host=lambda: _host(cpus=1),
        rank_catalog=rank_catalog_fastest,
        collect_prefix=forged_batch,
        clock=lambda: NOW,
    )
    with pytest.raises(CatalogPreviewError) as batch_error:
        build_catalog_preview(ports=batch_ports)
    assert batch_error.value.stage is CatalogPreviewStage.CHAIN
    assert batch_error.value.code is CatalogPreviewErrorCode.CHAIN_BATCH_INVALID


def test_custom_catalog_port_is_rejected_before_host_discovery():
    rows = tuple(load_packaged_full_puzzles())
    custom = snapshot_from_puzzles(rows)
    host_called = False

    def forbidden_host():
        nonlocal host_called
        host_called = True
        return _host()

    ports = CatalogPreviewPorts(
        load_catalog=lambda: custom,
        discover_host=forbidden_host,
        rank_catalog=rank_catalog_fastest,
        collect_prefix=collect_production_catalog_prefix,
        clock=lambda: NOW,
    )

    with pytest.raises(CatalogPreviewError) as error:
        build_catalog_preview(ports=ports)

    assert error.value.stage is CatalogPreviewStage.CATALOG
    assert error.value.code is CatalogPreviewErrorCode.CATALOG_AUTHORITY_INVALID
    assert not host_called


@pytest.mark.parametrize(
    ("failed_stage", "expected_code"),
    [
        (CatalogPreviewStage.CATALOG, CatalogPreviewErrorCode.CATALOG_LOAD_FAILED),
        (CatalogPreviewStage.HOST, CatalogPreviewErrorCode.HOST_DISCOVERY_FAILED),
        (CatalogPreviewStage.RANKING, CatalogPreviewErrorCode.RANKING_FAILED),
        (CatalogPreviewStage.CHAIN, CatalogPreviewErrorCode.CHAIN_COLLECTION_FAILED),
    ],
)
def test_dependency_exceptions_use_static_non_sensitive_mapping(failed_stage, expected_code):
    secret = "DO_NOT_ECHO_PRIVATE_KEY_OR_PROVIDER_SECRET"

    def fail(*_args):
        raise RuntimeError(secret)

    ports = CatalogPreviewPorts(
        load_catalog=fail if failed_stage is CatalogPreviewStage.CATALOG else load_snapshot,
        discover_host=fail if failed_stage is CatalogPreviewStage.HOST else _host,
        rank_catalog=fail if failed_stage is CatalogPreviewStage.RANKING else rank_catalog_fastest,
        collect_prefix=fail,
        clock=lambda: NOW,
    )

    with pytest.raises(CatalogPreviewError) as captured:
        build_catalog_preview(ports=ports)

    error = captured.value
    assert error.stage is failed_stage
    assert error.code is expected_code
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in error.detail
    assert secret not in error.remedy


def test_report_omits_sensitive_and_authority_material(monkeypatch):
    captured: dict[str, object] = {}
    report = build_catalog_preview(
        ports=_ports(
            monkeypatch,
            states=("confirmed",),
            captured=captured,
        )
    )
    ranking = captured["ranking"]
    selected_target = ranking.algorithmically_selectable[0].binding.target
    wire_txid = hashlib.sha256(b"preview-transaction-0").hexdigest()
    rendered = report.render_text()
    lowered = rendered.lower()

    assert selected_target.address not in rendered
    if selected_target.public_key_hex:
        assert selected_target.public_key_hex not in rendered
    assert wire_txid not in rendered
    assert "https://" not in rendered
    assert "address" not in lowered
    assert "pubkey" not in lowered
    assert "public_key" not in lowered
    assert "txid" not in lowered
    assert "recipe" not in lowered
    assert "decision" not in lowered
    assert "private_key" not in lowered
    assert "signed_tx" not in lowered


def test_preview_creates_no_workspace_files(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    report = build_catalog_preview(ports=_ports(monkeypatch, states=("confirmed",)))

    assert report.outcome is CatalogPreviewOutcome.SELECTED
    assert list(tmp_path.iterdir()) == []


def test_production_factory_is_direct_and_inert(monkeypatch):
    ports = production_catalog_preview_ports()
    assert ports.load_catalog is load_snapshot
    assert ports.discover_host is discover_host
    assert ports.rank_catalog is rank_catalog_fastest
    assert ports.collect_prefix is collect_production_catalog_prefix
    assert ports.clock is preview_mod._utc_now

    calls: list[str] = []

    def forbidden(*_args):
        calls.append("called")
        raise AssertionError("factory executed a production stage")

    monkeypatch.setattr(preview_mod, "load_snapshot", forbidden)
    monkeypatch.setattr(preview_mod, "discover_host", forbidden)
    monkeypatch.setattr(preview_mod, "rank_catalog_fastest", forbidden)
    monkeypatch.setattr(preview_mod, "collect_production_catalog_prefix", forbidden)
    monkeypatch.setattr(preview_mod, "_utc_now", forbidden)

    inert = production_catalog_preview_ports()

    assert calls == []
    assert inert.load_catalog is forbidden
    assert inert.discover_host is forbidden
    assert inert.rank_catalog is forbidden
    assert inert.collect_prefix is forbidden
    assert inert.clock is forbidden
