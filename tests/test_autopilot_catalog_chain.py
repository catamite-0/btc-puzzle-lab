from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from datetime import UTC, datetime
from urllib.parse import unquote

import pytest

import btc_puzzle_lab.autopilot.chain as chain_mod
from btc_puzzle_lab.autopilot.catalog_ranking import (
    CatalogFastestRankingReceipt,
    rank_catalog_fastest,
)
from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogSnapshotProvenance,
    load_snapshot,
)
from btc_puzzle_lab.autopilot.chain import (
    CatalogChainBatchOutcome,
    CatalogChainBatchReceipt,
    CatalogChainCandidateStatus,
    ChainAcquisitionError,
    ChainEvidenceProvenance,
    HttpTransportError,
    collect_production_catalog_prefix,
    is_catalog_chain_batch_receipt_issued,
    is_production_chain_admission_receipt_issued,
)
from btc_puzzle_lab.autopilot.facts import ChainPurpose, HostCapabilities
from btc_puzzle_lab.autopilot.planning import PlanningPolicy
from btc_puzzle_lab.crypto import address_hash160

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
GIB = 1024**3


def _ranking(*, cpus: int = 8) -> CatalogFastestRankingReceipt:
    return rank_catalog_fastest(
        load_snapshot(),
        HostCapabilities(
            architecture="x86_64",
            cpu_count=cpus,
            memory_bytes=16 * GIB,
            disk_free_bytes=100 * GIB,
        ),
        PlanningPolicy(),
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
    def __init__(
        self,
        ranking: CatalogFastestRankingReceipt,
        states: tuple[str, ...],
        *,
        share_first_two_transaction: bool = False,
    ) -> None:
        candidates = ranking.algorithmically_selectable
        assert len(states) <= len(candidates)
        self.claims: dict[str, dict[str, object]] = {}
        self.transactions: dict[str, bytes] = {}

        shared_txid = hashlib.sha256(b"shared-catalog-transaction").hexdigest()
        if share_first_two_transaction:
            assert len(states) >= 2
            shared_outputs = [
                {"scriptpubkey": _script(candidates[index].binding.target.address), "value": 1_000}
                for index in range(2)
            ]
            self.transactions[shared_txid] = _json_bytes(
                {
                    "status": _status(confirmed=False),
                    "txid": shared_txid,
                    "vout": shared_outputs,
                }
            )

        for index, candidate in enumerate(candidates):
            address = candidate.binding.target.address
            state = states[index] if index < len(states) else "empty"
            claim: dict[str, object] = {"state": state}
            if state in {"confirmed", "unconfirmed"}:
                shared = share_first_two_transaction and index < 2
                txid = (
                    shared_txid
                    if shared
                    else hashlib.sha256(f"catalog-tx-{index}".encode()).hexdigest()
                )
                vout = index if shared else 0
                confirmed = state == "confirmed"
                claim.update({"txid": txid, "vout": vout, "confirmed": confirmed})
                if not shared:
                    self.transactions[txid] = _json_bytes(
                        {
                            "status": _status(confirmed=confirmed),
                            "txid": txid,
                            "vout": [
                                {
                                    "scriptpubkey": _script(address),
                                    "value": 1_000,
                                }
                            ],
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
    def __init__(
        self,
        provider_id: str,
        wire: _WirePlan,
        *,
        fail_close: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.wire = wire
        self.fail_close = fail_close
        self.trust_env = True
        self.closed = False
        self.close_count = 0
        self.calls: list[tuple[str, str]] = []
        self.responses: list[_StreamingResponse] = []

    def get(self, url: str, **kwargs: object) -> _StreamingResponse:
        expected_origin = {
            "mempool-space": "https://mempool.space/api/",
            "blockstream-info": "https://blockstream.info/api/",
        }[self.provider_id]
        assert url.startswith(expected_origin)
        assert self.trust_env is False
        assert kwargs["allow_redirects"] is False
        assert kwargs["stream"] is True
        assert kwargs["headers"]["Accept-Encoding"] == "identity"  # type: ignore[index]

        if "/address/" in url:
            address = unquote(url.split("/address/", 1)[1].removesuffix("/utxo"))
            claim = self.wire.claims[address]
            self.calls.append(("utxos", address))
            if claim["state"] == "unknown":
                response = _StreamingResponse(503, b"unavailable")
            elif claim["state"] == "empty":
                response = _StreamingResponse(200, b"[]")
            else:
                confirmed = bool(claim["confirmed"])
                response = _StreamingResponse(
                    200,
                    _json_bytes(
                        [
                            {
                                "status": _status(confirmed=confirmed),
                                "txid": claim["txid"],
                                "value": 1_000,
                                "vout": claim["vout"],
                            }
                        ]
                    ),
                )
        elif url.endswith("/blocks/tip/height"):
            self.calls.append(("tip", ""))
            tip = 963_000 if self.provider_id == "mempool-space" else 963_001
            response = _StreamingResponse(200, f"{tip}\n".encode())
        else:
            txid = url.rsplit("/", 1)[-1]
            self.calls.append(("transaction", txid))
            response = _StreamingResponse(200, self.wire.transactions[txid])
        self.responses.append(response)
        return response

    def close(self) -> None:
        self.closed = True
        self.close_count += 1
        if self.fail_close:
            raise RuntimeError("close failed")


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    wire: _WirePlan,
    *,
    fail_close: tuple[bool, bool] = (False, False),
) -> tuple[_BatchSession, _BatchSession]:
    sessions = (
        _BatchSession("mempool-space", wire, fail_close=fail_close[0]),
        _BatchSession("blockstream-info", wire, fail_close=fail_close[1]),
    )
    pending = iter(sessions)
    monkeypatch.setattr(chain_mod, "_new_requests_session", lambda: next(pending))
    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)
    monkeypatch.setattr(chain_mod, "_monotonic_seconds", lambda: 0.0)
    return sessions


def test_confirmed_candidate_selects_and_marks_exact_ranked_suffix_not_checked(monkeypatch):
    ranking = _ranking()
    sessions = _patch_runtime(monkeypatch, _WirePlan(ranking, ("confirmed",)))

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.SELECTED
    assert receipt.selected_target_id == ranking.algorithmically_selectable[0].puzzle_id
    assert receipt.checked_count == 1
    assert receipt.candidates[0].status is CatalogChainCandidateStatus.FUNDED_CONFIRMED
    assert all(
        candidate.status is CatalogChainCandidateStatus.NOT_CHECKED
        for candidate in receipt.candidates[1:]
    )
    assert receipt.catalog_provenance is CatalogSnapshotProvenance.PACKAGE_V1
    assert receipt.provenance is ChainEvidenceProvenance.PRODUCTION_CATALOG_HTTP_V1
    assert receipt.ranking is ranking
    assert receipt.prefix_receipts == (receipt.selected_receipt,)
    assert is_production_chain_admission_receipt_issued(receipt.selected_receipt)
    assert is_catalog_chain_batch_receipt_issued(receipt)
    assert all(session.closed and session.trust_env is False for session in sessions)


def test_empty_and_unconfirmed_continue_until_first_confirmed_candidate(monkeypatch):
    ranking = _ranking()
    _patch_runtime(
        monkeypatch,
        _WirePlan(ranking, ("empty", "unconfirmed", "confirmed")),
    )

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.SELECTED
    assert receipt.checked_count == 3
    assert tuple(candidate.status for candidate in receipt.candidates[:4]) == (
        CatalogChainCandidateStatus.EMPTY,
        CatalogChainCandidateStatus.FUNDED_UNCONFIRMED,
        CatalogChainCandidateStatus.FUNDED_CONFIRMED,
        CatalogChainCandidateStatus.NOT_CHECKED,
    )
    assert receipt.selected_target_id == ranking.algorithmically_selectable[2].puzzle_id


def test_unknown_stops_immediately_and_never_selects_a_later_funded_target(monkeypatch):
    ranking = _ranking()
    sessions = _patch_runtime(
        monkeypatch,
        _WirePlan(ranking, ("empty", "unknown", "confirmed")),
    )

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.INDETERMINATE
    assert receipt.selected_target_id is None
    assert receipt.checked_count == 2
    assert receipt.candidates[1].status is CatalogChainCandidateStatus.UNKNOWN
    assert receipt.candidates[2].status is CatalogChainCandidateStatus.NOT_CHECKED
    third_address = ranking.algorithmically_selectable[2].binding.target.address
    assert all(("utxos", third_address) not in session.calls for session in sessions)


def test_all_empty_ranked_candidates_exhaust_to_no_feasible(monkeypatch):
    ranking = _ranking()
    sessions = _patch_runtime(monkeypatch, _WirePlan(ranking, ()))

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.NO_FEASIBLE
    assert receipt.checked_count == len(ranking.algorithmically_selectable) == 78
    assert all(
        candidate.status is CatalogChainCandidateStatus.EMPTY for candidate in receipt.candidates
    )
    assert receipt.request_count == 158
    assert tuple(item.request_count for item in receipt.provider_counts) == (79, 79)
    assert all(session.closed for session in sessions)


def test_no_algorithmically_selectable_target_performs_no_http_or_monotonic_io(monkeypatch):
    ranking = _ranking(cpus=1)
    assert ranking.algorithmically_selectable == ()
    monkeypatch.setattr(
        chain_mod,
        "_new_requests_session",
        lambda: (_ for _ in ()).throw(AssertionError("no session expected")),
    )
    monkeypatch.setattr(
        chain_mod,
        "_monotonic_seconds",
        lambda: (_ for _ in ()).throw(AssertionError("no HTTP budget expected")),
    )
    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.NO_FEASIBLE
    assert receipt.candidates == ()
    assert receipt.checked_count == receipt.request_count == 0
    assert tuple(item.request_count for item in receipt.provider_counts) == (0, 0)


def test_provider_local_tip_and_transaction_caches_are_reused_but_not_shared(monkeypatch):
    ranking = _ranking()
    wire = _WirePlan(
        ranking,
        ("unconfirmed", "unconfirmed", "confirmed"),
        share_first_two_transaction=True,
    )
    sessions = _patch_runtime(monkeypatch, wire)

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.SELECTED
    assert tuple(item.request_count for item in receipt.provider_counts) == (6, 6)
    assert tuple(item.unique_transaction_count for item in receipt.provider_counts) == (2, 2)
    for session in sessions:
        resources = [resource for resource, _ in session.calls]
        assert resources.count("tip") == 1
        assert resources.count("transaction") == 2
        assert resources.count("utxos") == 3
    assert sessions[0] is not sessions[1]


def test_shared_request_budget_and_unique_transaction_cap_fail_to_indeterminate(monkeypatch):
    ranking = _ranking()
    sessions = _patch_runtime(monkeypatch, _WirePlan(ranking, ("empty",)))
    monkeypatch.setattr(chain_mod, "_CATALOG_HTTP_REQUEST_LIMIT", 1)

    limited = collect_production_catalog_prefix(ranking)

    assert limited.outcome is CatalogChainBatchOutcome.INDETERMINATE
    assert limited.request_count == 1
    assert sum(len(session.calls) for session in sessions) == 1

    ranking = _ranking()
    sessions = _patch_runtime(
        monkeypatch,
        _WirePlan(ranking, ("unconfirmed", "unconfirmed")),
    )
    monkeypatch.setattr(chain_mod, "_CATALOG_HTTP_REQUEST_LIMIT", 400)
    monkeypatch.setattr(chain_mod, "_CATALOG_PROVIDER_MAX_UNIQUE_TRANSACTIONS", 1)

    capped = collect_production_catalog_prefix(ranking)

    assert capped.outcome is CatalogChainBatchOutcome.INDETERMINATE
    assert capped.checked_count == 2
    assert tuple(item.unique_transaction_count for item in capped.provider_counts) == (1, 1)
    assert all(session.closed for session in sessions)


def test_per_provider_byte_budget_is_enforced_before_body_read(monkeypatch):
    ranking = _ranking()
    sessions = _patch_runtime(monkeypatch, _WirePlan(ranking, ("empty",)))
    monkeypatch.setattr(chain_mod, "_CATALOG_PROVIDER_TOTAL_BYTES_LIMIT", 1)

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.INDETERMINATE
    assert tuple(item.request_count for item in receipt.provider_counts) == (1, 1)
    assert tuple(item.decompressed_bytes for item in receipt.provider_counts) == (0, 0)
    assert all(response.closed for session in sessions for response in session.responses)


def test_total_monotonic_deadline_covers_payload_parsing_and_closes_sessions(monkeypatch):
    ranking = _ranking()
    sessions = _patch_runtime(monkeypatch, _WirePlan(ranking, ("empty",)))
    monotonic = [0.0]
    monkeypatch.setattr(chain_mod, "_monotonic_seconds", lambda: monotonic[0])
    parse_claims = chain_mod._canonical_esplora_claims
    parse_count = [0]

    def expire_after_parse(payload: bytes):
        claims = parse_claims(payload)
        parse_count[0] += 1
        if parse_count[0] == 2:
            monotonic[0] = 121.0
        return claims

    monkeypatch.setattr(chain_mod, "_canonical_esplora_claims", expire_after_parse)

    with pytest.raises(HttpTransportError, match="deadline"):
        collect_production_catalog_prefix(ranking)

    assert all(session.closed for session in sessions)


def test_both_sessions_are_closed_even_when_one_close_fails(monkeypatch):
    ranking = _ranking()
    sessions = _patch_runtime(
        monkeypatch,
        _WirePlan(ranking, ("confirmed",)),
        fail_close=(True, False),
    )

    with pytest.raises(HttpTransportError, match="close failed"):
        collect_production_catalog_prefix(ranking)

    assert tuple(session.close_count for session in sessions) == (1, 1)
    assert all(session.closed for session in sessions)


def test_duplicate_session_factory_fails_closed_and_closes_once(monkeypatch):
    ranking = _ranking()
    session = _BatchSession("mempool-space", _WirePlan(ranking, ("confirmed",)))
    monkeypatch.setattr(chain_mod, "_new_requests_session", lambda: session)
    monkeypatch.setattr(chain_mod, "_utc_now", lambda: NOW)
    monkeypatch.setattr(chain_mod, "_monotonic_seconds", lambda: 0.0)

    with pytest.raises(HttpTransportError, match="distinct HTTP sessions"):
        collect_production_catalog_prefix(ranking)

    assert session.close_count == 1


def test_forged_reordered_or_modified_authority_is_rejected_before_http(monkeypatch):
    ranking = _ranking()
    monkeypatch.setattr(
        chain_mod,
        "_new_requests_session",
        lambda: (_ for _ in ()).throw(AssertionError("invalid ranking must not use HTTP")),
    )

    forged = object.__new__(CatalogFastestRankingReceipt)
    for item in fields(CatalogFastestRankingReceipt):
        object.__setattr__(forged, item.name, getattr(ranking, item.name))
    with pytest.raises(ChainAcquisitionError, match="exact issued"):
        collect_production_catalog_prefix(forged)

    reordered = tuple(reversed(ranking.algorithmically_selectable))
    object.__setattr__(ranking, "algorithmically_selectable", reordered)
    with pytest.raises(ChainAcquisitionError, match="exact issued"):
        collect_production_catalog_prefix(ranking)


def test_batch_constructor_copy_and_post_issue_modification_have_no_authority(monkeypatch):
    ranking = _ranking()
    _patch_runtime(monkeypatch, _WirePlan(ranking, ("confirmed",)))
    receipt = collect_production_catalog_prefix(ranking)

    with pytest.raises(ChainAcquisitionError, match="sealed production"):
        CatalogChainBatchReceipt(
            ranking=ranking,
            outcome=receipt.outcome,
            candidates=receipt.candidates,
            provider_counts=receipt.provider_counts,
            batch_started_at=receipt.batch_started_at,
            batch_completed_at=receipt.batch_completed_at,
        )

    object.__setattr__(receipt, "selected_target_id", 999)
    assert not is_catalog_chain_batch_receipt_issued(receipt)


def test_collection_creates_no_workspace_files(monkeypatch, tmp_path):
    ranking = _ranking()
    _patch_runtime(monkeypatch, _WirePlan(ranking, ("confirmed",)))
    monkeypatch.chdir(tmp_path)

    receipt = collect_production_catalog_prefix(ranking)

    assert receipt.outcome is CatalogChainBatchOutcome.SELECTED
    assert list(tmp_path.iterdir()) == []
    assert receipt.purpose is ChainPurpose.SELECTION


def test_catalog_prefix_api_accepts_no_caller_owned_transport_or_budget():
    ranking = _ranking()
    with pytest.raises(TypeError):
        collect_production_catalog_prefix(  # type: ignore[call-arg]
            ranking,
            transport=object(),
        )
