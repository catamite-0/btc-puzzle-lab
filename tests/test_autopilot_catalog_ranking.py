from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import btc_puzzle_lab.autopilot.catalog_view as catalog_view_mod
from btc_puzzle_lab.autopilot.catalog_ranking import (
    CATALOG_FASTEST_OBJECTIVE_V1,
    CatalogFastestRankingReceipt,
    CatalogRankingErrorCode,
    CatalogRankingValidationError,
    is_catalog_fastest_ranking_receipt_issued,
    rank_catalog_fastest,
)
from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogSnapshot,
    CatalogSnapshotProvenance,
    is_packaged_catalog_snapshot_issued,
    load_snapshot,
    snapshot_from_puzzles,
)
from btc_puzzle_lab.autopilot.chain import (
    ChainAdmissionReceipt,
    FixtureProvider,
    ProviderRegistry,
    ProviderResource,
    RawHttpResponse,
    collect_chain_evidence,
)
from btc_puzzle_lab.autopilot.facts import (
    ChainPurpose,
    EngineName,
    HostCapabilities,
    PuzzleTarget,
    TargetMode,
)
from btc_puzzle_lab.autopilot.planning import (
    PlanningPolicy,
    algorithm_assessment_fingerprint,
    plan_target,
)
from btc_puzzle_lab.catalog import Puzzle, load_packaged_full_puzzles
from btc_puzzle_lab.crypto import address_hash160

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
GIB = 1024**3
TXID = "11" * 32


def _full_snapshot():
    return load_snapshot()


def _host(*, cpus: int = 8) -> HostCapabilities:
    return HostCapabilities(
        architecture="x86_64",
        cpu_count=cpus,
        memory_bytes=16 * GIB,
        disk_free_bytes=100 * GIB,
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _write_catalog(path: Path, puzzles: tuple[Puzzle, ...]) -> None:
    rows = [
        {
            "id": puzzle.id,
            "bits": puzzle.bits,
            "address": puzzle.address,
            "range_start_hex": puzzle.range_start_hex,
            "range_end_hex": puzzle.range_end_hex,
            "pubkey_compressed_hex": puzzle.pubkey_compressed_hex,
            "practice_solution_hex": (
                None if puzzle.practice_solution is None else f"{puzzle.practice_solution:x}"
            ),
            "status": puzzle.status,
            "engine_default": puzzle.engine_default,
            "notes": puzzle.notes,
        }
        for puzzle in puzzles
    ]
    path.write_text(json.dumps({"puzzles": rows}), encoding="utf-8")


class _FundedTransport:
    def __init__(self, script_pubkey_hex: str) -> None:
        self.script_pubkey_hex = script_pubkey_hex

    def get(
        self,
        *,
        provider_id: str,
        resource: ProviderResource,
        address: str,
        txid: str | None = None,
    ) -> RawHttpResponse:
        del address, txid
        if resource is ProviderResource.TIP:
            key = "tip_height" if provider_id == FixtureProvider.ALPHA else "height"
            return RawHttpResponse(200, _json_bytes({key: 963_000}))
        if provider_id == FixtureProvider.ALPHA:
            payload = {
                "utxos": [
                    {
                        "confirmed": True,
                        "script_pubkey_hex": self.script_pubkey_hex,
                        "txid": TXID,
                        "value_sats": 100_000,
                        "vout": 0,
                    }
                ]
            }
        else:
            payload = {
                "outputs": [
                    {
                        "is_confirmed": True,
                        "locking_script": self.script_pubkey_hex,
                        "output_index": 0,
                        "satoshis": 100_000,
                        "transaction_id": TXID,
                    }
                ]
            }
        return RawHttpResponse(200, _json_bytes(payload))


def _funded_receipt(target: PuzzleTarget) -> ChainAdmissionReceipt:
    script = (b"\x76\xa9\x14" + address_hash160(target.address) + b"\x88\xac").hex()
    evidence = collect_chain_evidence(
        target=target,
        purpose=ChainPurpose.SELECTION,
        registry=ProviderRegistry.fixture(),
        transport=_FundedTransport(script),
        clock=lambda: NOW,
    )
    assert type(evidence) is ChainAdmissionReceipt
    return evidence


def _candidate(receipt: CatalogFastestRankingReceipt, puzzle_id: int):
    return next(
        candidate
        for candidate in receipt.algorithmically_selectable
        if candidate.puzzle_id == puzzle_id
    )


def test_complete_public_snapshot_issues_total_live_ranking_and_excludes_practice():
    snapshot = _full_snapshot()
    host = _host()
    policy = PlanningPolicy()

    receipt = rank_catalog_fastest(snapshot, host, policy)

    expected_live_ids = tuple(
        entry.target.puzzle_id for entry in snapshot.entries if entry.target.mode is TargetMode.LIVE
    )
    assert is_catalog_fastest_ranking_receipt_issued(receipt)
    assert receipt.catalog_fingerprint == snapshot.catalog_fingerprint
    assert receipt.catalog_provenance is CatalogSnapshotProvenance.PACKAGE_V1
    assert is_packaged_catalog_snapshot_issued(snapshot)
    assert receipt.preview_host is host
    assert receipt.host_fingerprint == host.fingerprint
    assert receipt.policy_fingerprint == policy.policy_fingerprint
    assert receipt.objective == CATALOG_FASTEST_OBJECTIVE_V1
    assert receipt.purpose is ChainPurpose.SELECTION
    assert not receipt.executable
    assert receipt.candidate_ids == expected_live_ids
    assert len(receipt.candidate_ids) == 78
    assert 22 not in receipt.candidate_ids
    assert len(receipt.algorithmically_selectable) + len(receipt.statically_blocked) == 78
    assert (
        tuple(
            sorted(
                receipt.algorithmically_selectable,
                key=lambda candidate: candidate.order_key(),
            )
        )
        == receipt.algorithmically_selectable
    )

    puzzle_135 = _candidate(receipt, 135)
    assert puzzle_135.binding.target.mode is TargetMode.LIVE
    assert puzzle_135.selected_for_comparison.engine is EngineName.KANGAROO


def test_static_blocked_targets_are_retained_separately_in_id_order():
    receipt = rank_catalog_fastest(_full_snapshot(), _host(cpus=1), PlanningPolicy())

    assert receipt.algorithmically_selectable == ()
    assert tuple(candidate.puzzle_id for candidate in receipt.statically_blocked) == (
        receipt.candidate_ids
    )
    assert all(
        tuple(item.engine for item in candidate.algorithm_blockers) == tuple(EngineName)
        for candidate in receipt.statically_blocked
    )


def test_ranking_is_independent_of_package_adapter_row_input_order(monkeypatch):
    rows = tuple(load_packaged_full_puzzles())
    monkeypatch.setattr(catalog_view_mod, "load_packaged_full_puzzles", lambda: list(rows))
    forward = load_snapshot()
    monkeypatch.setattr(
        catalog_view_mod,
        "load_packaged_full_puzzles",
        lambda: list(reversed(rows)),
    )
    reverse = load_snapshot()
    host = _host()
    policy = PlanningPolicy(planning_horizon_seconds=60)

    forward_receipt = rank_catalog_fastest(forward, host, policy)
    reverse_receipt = rank_catalog_fastest(reverse, host, policy)

    assert forward.catalog_fingerprint == reverse.catalog_fingerprint
    assert forward_receipt.candidate_ids == reverse_receipt.candidate_ids
    assert tuple(
        (candidate.puzzle_id, candidate.selected_for_comparison.engine)
        for candidate in forward_receipt.algorithmically_selectable
    ) == tuple(
        (candidate.puzzle_id, candidate.selected_for_comparison.engine)
        for candidate in reverse_receipt.algorithmically_selectable
    )
    assert forward_receipt.ranking_fingerprint == reverse_receipt.ranking_fingerprint


def test_incomplete_or_forged_snapshot_cannot_issue_a_catalog_ranking(monkeypatch):
    rows = tuple(load_packaged_full_puzzles())
    package_loader = catalog_view_mod.load_packaged_full_puzzles
    monkeypatch.setattr(catalog_view_mod, "load_packaged_full_puzzles", lambda: list(rows[:-1]))
    incomplete = load_snapshot()
    with pytest.raises(CatalogRankingValidationError) as missing:
        rank_catalog_fastest(incomplete, _host(), PlanningPolicy())
    assert missing.value.code is CatalogRankingErrorCode.SNAPSHOT_NOT_COMPLETE

    monkeypatch.setattr(catalog_view_mod, "load_packaged_full_puzzles", package_loader)
    issued = load_snapshot()
    forged = object.__new__(CatalogSnapshot)
    object.__setattr__(forged, "entries", issued.entries)
    object.__setattr__(forged, "catalog_fingerprint", issued.catalog_fingerprint)
    object.__setattr__(forged, "provenance", issued.provenance)
    with pytest.raises(CatalogRankingValidationError) as fake:
        rank_catalog_fastest(forged, _host(), PlanningPolicy())
    assert fake.value.code is CatalogRankingErrorCode.SNAPSHOT_NOT_ISSUED


def test_receipt_constructor_copy_and_post_issue_modification_have_no_authority():
    receipt = rank_catalog_fastest(_full_snapshot(), _host(), PlanningPolicy())
    constructor_fields = {
        "catalog_fingerprint": receipt.catalog_fingerprint,
        "catalog_provenance": receipt.catalog_provenance,
        "preview_host": receipt.preview_host,
        "host_fingerprint": receipt.host_fingerprint,
        "policy_fingerprint": receipt.policy_fingerprint,
        "objective": receipt.objective,
        "purpose": receipt.purpose,
        "candidate_ids": receipt.candidate_ids,
        "algorithmically_selectable": receipt.algorithmically_selectable,
        "statically_blocked": receipt.statically_blocked,
    }
    with pytest.raises(CatalogRankingValidationError) as direct:
        CatalogFastestRankingReceipt(**constructor_fields)
    assert direct.value.code is CatalogRankingErrorCode.INVALID_REQUEST

    forged = object.__new__(CatalogFastestRankingReceipt)
    for item in fields(CatalogFastestRankingReceipt):
        object.__setattr__(forged, item.name, getattr(receipt, item.name))
    assert not is_catalog_fastest_ranking_receipt_issued(forged)

    object.__setattr__(receipt, "candidate_ids", receipt.candidate_ids[:-1])
    assert not is_catalog_fastest_ranking_receipt_issued(receipt)


def test_structurally_complete_modified_and_custom_path_snapshots_are_not_package_authority(
    tmp_path: Path,
):
    rows = tuple(load_packaged_full_puzzles())
    changed = tuple(
        replace(puzzle, range_end=puzzle.range_end - 1) if puzzle.id == 135 else puzzle
        for puzzle in rows
    )
    descriptive = snapshot_from_puzzles(changed)
    assert descriptive.provenance is CatalogSnapshotProvenance.DESCRIPTIVE_V1
    assert not is_packaged_catalog_snapshot_issued(descriptive)
    with pytest.raises(CatalogRankingValidationError) as modified:
        rank_catalog_fastest(descriptive, _host(), PlanningPolicy())
    assert modified.value.code is CatalogRankingErrorCode.SNAPSHOT_NOT_PACKAGED

    custom_path = tmp_path / "complete-custom-catalog.json"
    _write_catalog(custom_path, rows)
    custom = load_snapshot(custom_path)
    assert custom.provenance is CatalogSnapshotProvenance.CUSTOM_PATH_V1
    assert not is_packaged_catalog_snapshot_issued(custom)
    with pytest.raises(CatalogRankingValidationError) as explicit:
        rank_catalog_fastest(custom, _host(), PlanningPolicy())
    assert explicit.value.code is CatalogRankingErrorCode.SNAPSHOT_NOT_PACKAGED


def test_catalog_selected_algorithm_matches_plan_target_exactly_for_puzzle_135():
    snapshot = _full_snapshot()
    host = _host()
    policy = PlanningPolicy(planning_horizon_seconds=600)
    ranking = rank_catalog_fastest(snapshot, host, policy)
    candidate = _candidate(ranking, 135)

    single = plan_target(
        candidate.binding,
        _funded_receipt(candidate.binding.target),
        host,
        policy=policy,
        evaluated_at=NOW,
    )

    assert single.decision is not None
    selected = single.decision.selected
    detached = candidate.selected_for_comparison
    assert detached.engine is selected.engine
    assert detached.resource is selected.resource
    assert detached.provisioning is selected.provisioning
    assert detached.assessment_fingerprint == algorithm_assessment_fingerprint(selected)
    assert detached.full_solution_eta_seconds.as_fraction() == (
        selected.estimate.full_solution_eta_seconds
    )
    assert detached.estimate_confidence.as_fraction() == selected.estimate.confidence


def test_public_total_order_includes_engine_as_the_final_exact_tie_break():
    receipt = rank_catalog_fastest(_full_snapshot(), _host(), PlanningPolicy())

    assert all(len(candidate.order_key()) == 4 for candidate in receipt.algorithmically_selectable)
    assert all(
        candidate.order_key()[2:]
        == (
            candidate.puzzle_id,
            candidate.selected_for_comparison.engine.value,
        )
        for candidate in receipt.algorithmically_selectable
    )
