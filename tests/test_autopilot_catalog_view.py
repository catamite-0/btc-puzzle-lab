from __future__ import annotations

import copy
import json
import pickle
from dataclasses import fields as dataclass_fields
from dataclasses import replace
from pathlib import Path

import pytest

from btc_puzzle_lab.autopilot.catalog_view import (
    CatalogSnapshot,
    CatalogSnapshotProvenance,
    CatalogTargetBinding,
    CatalogTargetEntry,
    CatalogTargetError,
    PracticeFixtureEvidence,
    entry_from_puzzle,
    is_catalog_snapshot_issued,
    is_catalog_target_binding_issued,
    is_packaged_catalog_snapshot_issued,
    is_practice_fixture_evidence_issued,
    load_snapshot,
    snapshot_from_puzzles,
    target_from_puzzle,
)
from btc_puzzle_lab.autopilot.facts import TargetMode
from btc_puzzle_lab.catalog import Puzzle


def _puzzle(**changes) -> Puzzle:
    base = Puzzle(
        id=1,
        bits=1,
        address="1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH",
        range_start=1,
        range_end=1,
        pubkey_compressed_hex=(
            "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
        ),
        practice_solution=1,
        status="solved",
        engine_default="sequential",
        notes="public practice fixture",
    )
    return replace(base, **changes)


def _live_puzzle(**changes) -> Puzzle:
    base = Puzzle(
        id=2,
        bits=2,
        address="1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb",
        range_start=2,
        range_end=3,
        pubkey_compressed_hex="",
        practice_solution=None,
        status="unsolved",
        engine_default="window",
        notes="live target",
    )
    return replace(base, **changes)


def _write_catalog(path: Path, puzzles: tuple[Puzzle, ...]) -> None:
    rows = []
    for puzzle in puzzles:
        rows.append(
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
        )
    path.write_text(json.dumps({"puzzles": rows}), encoding="utf-8")


def test_solved_row_becomes_practice_without_solution_material() -> None:
    target = target_from_puzzle(_puzzle())

    assert target.mode is TargetMode.PRACTICE
    assert target.practice_fixture_id == "package-catalog-v1:puzzle-1"
    assert not hasattr(target, "practice_solution")

    entry = entry_from_puzzle(_puzzle())
    assert isinstance(entry, CatalogTargetEntry)
    assert isinstance(entry.practice_fixture, PracticeFixtureEvidence)
    assert entry.practice_fixture.matches(target)
    assert len(entry.practice_fixture.fixture_fingerprint) == 64
    assert "practice_solution" not in repr(entry.practice_fixture)


def test_unsolved_row_becomes_live() -> None:
    target = target_from_puzzle(
        _puzzle(
            id=2,
            bits=2,
            range_start=2,
            range_end=3,
            address="1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb",
            pubkey_compressed_hex="",
            practice_solution=None,
            status="unsolved",
        )
    )

    assert target.mode is TargetMode.LIVE
    assert target.practice_fixture_id is None


def test_default_snapshot_uses_complete_package_catalog() -> None:
    snapshot = load_snapshot()
    binding_71 = snapshot.bind_target(71)
    binding_140 = snapshot.bind_target(140)

    assert len(snapshot.entries) == 160
    assert sum(target.mode is TargetMode.LIVE for target in snapshot.targets) == 78
    assert binding_71.target.mode is TargetMode.LIVE
    assert binding_140.target.mode is TargetMode.LIVE
    assert snapshot.provenance is CatalogSnapshotProvenance.PACKAGE_V1
    assert is_packaged_catalog_snapshot_issued(snapshot)


@pytest.mark.parametrize(
    ("puzzle", "message"),
    [
        (_puzzle(status="unknown"), "unsupported catalog status"),
        (_puzzle(status="unsolved"), "marked unsolved but carries"),
        (_puzzle(practice_solution=None), "has no public practice fixture"),
        (_puzzle(practice_solution=2), "outside its key range"),
        (
            _puzzle(
                range_end=2,
                practice_solution=2,
                pubkey_compressed_hex="",
            ),
            "does not match its address",
        ),
        (_puzzle(range_start=2, range_end=1), "invalid key range"),
    ],
)
def test_contradictory_catalog_rows_fail_closed(puzzle: Puzzle, message: str) -> None:
    with pytest.raises(CatalogTargetError, match=message):
        target_from_puzzle(puzzle)


def test_load_snapshot_rejects_duplicate_ids(tmp_path) -> None:
    catalog = tmp_path / "puzzles.json"
    catalog.write_text(
        """{
  "puzzles": [
    {
      "id": 2,
      "bits": 2,
      "address": "1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb",
      "range_start_hex": "2",
      "range_end_hex": "3",
      "pubkey_compressed_hex": "",
      "practice_solution_hex": null,
      "status": "unsolved",
      "engine_default": "window",
      "notes": ""
    },
    {
      "id": 2,
      "bits": 2,
      "address": "1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb",
      "range_start_hex": "2",
      "range_end_hex": "3",
      "pubkey_compressed_hex": "",
      "practice_solution_hex": null,
      "status": "unsolved",
      "engine_default": "window",
      "notes": ""
    }
  ]
}\n""",
        encoding="utf-8",
    )

    with pytest.raises(CatalogTargetError, match="duplicate puzzle ids"):
        load_snapshot(catalog)


def test_practice_evidence_cannot_be_constructed_or_rebound_by_callers() -> None:
    target = target_from_puzzle(_puzzle())
    with pytest.raises(CatalogTargetError, match="catalog validation"):
        PracticeFixtureEvidence(target=target, fixture_fingerprint="a" * 64)

    evidence = entry_from_puzzle(_puzzle()).practice_fixture
    assert evidence is not None
    forged = replace(
        target,
        puzzle_id=2,
        practice_fixture_id="caller-asserted-practice",
    )
    assert not evidence.matches(forged)


def test_practice_signing_rejects_string_subclasses_that_lie_about_address_equality() -> None:
    class LyingAddress(str):
        def __eq__(self, _other):
            return True

    live_address = LyingAddress("1CUNEBjYrCn2y1SdiUMohaKUi4wpP326Lb")
    forged_row = _puzzle(
        address=live_address,
        pubkey_compressed_hex="",
    )

    with pytest.raises(CatalogTargetError, match="exact strings"):
        entry_from_puzzle(forged_row)


def test_snapshot_is_canonical_and_binds_the_selected_target(tmp_path: Path) -> None:
    forward = tmp_path / "forward.json"
    reversed_path = tmp_path / "reversed.json"
    puzzles = (_puzzle(), _live_puzzle())
    _write_catalog(forward, puzzles)
    _write_catalog(reversed_path, tuple(reversed(puzzles)))

    snapshot = load_snapshot(forward)
    reordered = load_snapshot(reversed_path)
    binding = snapshot.bind_target(1)

    assert isinstance(snapshot, CatalogSnapshot)
    assert tuple(target.puzzle_id for target in snapshot.targets) == (1, 2)
    assert snapshot.catalog_fingerprint == reordered.catalog_fingerprint
    assert binding.target is snapshot.entries[0].target
    assert binding.practice_fixture is snapshot.entries[0].practice_fixture
    assert binding.catalog_fingerprint == snapshot.catalog_fingerprint
    assert is_catalog_snapshot_issued(snapshot)
    assert snapshot.provenance is CatalogSnapshotProvenance.CUSTOM_PATH_V1
    assert not is_packaged_catalog_snapshot_issued(snapshot)
    assert is_catalog_target_binding_issued(binding)
    assert is_practice_fixture_evidence_issued(binding.practice_fixture)


def test_selected_target_change_changes_snapshot_and_binding(tmp_path: Path) -> None:
    original_path = tmp_path / "original.json"
    changed_path = tmp_path / "changed.json"
    _write_catalog(original_path, (_puzzle(), _live_puzzle()))
    _write_catalog(changed_path, (_puzzle(), _live_puzzle(range_end=4)))

    original = load_snapshot(original_path).bind_target(2)
    changed = load_snapshot(changed_path).bind_target(2)

    assert original.target != changed.target
    assert original.catalog_fingerprint != changed.catalog_fingerprint


@pytest.mark.parametrize(
    "unrelated_change",
    [
        {"status": " UNSOLVED "},
        {"engine_default": "sequential"},
        {"notes": "changed metadata"},
    ],
)
def test_any_unrelated_catalog_row_change_invalidates_binding(
    tmp_path: Path,
    unrelated_change: dict[str, object],
) -> None:
    original_path = tmp_path / "original.json"
    changed_path = tmp_path / "changed.json"
    _write_catalog(original_path, (_puzzle(), _live_puzzle()))
    _write_catalog(changed_path, (_puzzle(), _live_puzzle(**unrelated_change)))

    original = load_snapshot(original_path).bind_target(1)
    changed = load_snapshot(changed_path).bind_target(1)

    assert original.target == changed.target
    assert original.practice_fixture == changed.practice_fixture
    assert original.catalog_fingerprint != changed.catalog_fingerprint


def test_snapshot_fingerprint_binds_public_practice_solution(tmp_path: Path) -> None:
    catalog = tmp_path / "puzzles.json"
    _write_catalog(catalog, (_puzzle(),))

    # This pins the canonical material, including the source-only public solution.
    assert load_snapshot(catalog).catalog_fingerprint == (
        "2b6f1fa13cf02eab5c1b3ee60785ccd039383affd42e0f973095bd2af4f99ef8"
    )


def test_target_binding_cannot_be_constructed_or_replaced_by_callers() -> None:
    entry = entry_from_puzzle(_puzzle())
    with pytest.raises(CatalogTargetError, match="catalog snapshot"):
        CatalogTargetBinding(
            target=entry.target,
            practice_fixture=entry.practice_fixture,
            catalog_fingerprint="a" * 64,
        )


def test_snapshot_and_binding_require_unchanged_process_local_issuance() -> None:
    snapshot = snapshot_from_puzzles((_puzzle(), _live_puzzle()))
    binding = snapshot.bind_target(1)

    assert snapshot.provenance is CatalogSnapshotProvenance.DESCRIPTIVE_V1
    assert not is_packaged_catalog_snapshot_issued(snapshot)

    forged_snapshot = object.__new__(CatalogSnapshot)
    object.__setattr__(forged_snapshot, "entries", snapshot.entries)
    object.__setattr__(forged_snapshot, "catalog_fingerprint", snapshot.catalog_fingerprint)
    assert not is_catalog_snapshot_issued(forged_snapshot)
    with pytest.raises(CatalogTargetError, match="not issued or was modified"):
        forged_snapshot.bind_target(1)

    for copied_snapshot in (copy.deepcopy(snapshot), pickle.loads(pickle.dumps(snapshot))):
        assert not is_catalog_snapshot_issued(copied_snapshot)
        with pytest.raises(CatalogTargetError, match="not issued or was modified"):
            copied_snapshot.bind_target(1)

    forged_binding = object.__new__(CatalogTargetBinding)
    object.__setattr__(forged_binding, "target", binding.target)
    object.__setattr__(forged_binding, "practice_fixture", binding.practice_fixture)
    object.__setattr__(forged_binding, "catalog_fingerprint", binding.catalog_fingerprint)
    assert not is_catalog_target_binding_issued(forged_binding)
    assert not is_catalog_target_binding_issued(copy.deepcopy(binding))
    assert not is_catalog_target_binding_issued(pickle.loads(pickle.dumps(binding)))
    with pytest.raises(CatalogTargetError, match="catalog snapshot"):
        replace(binding)

    object.__setattr__(binding, "catalog_fingerprint", "f" * 64)
    assert not is_catalog_target_binding_issued(binding)

    object.__setattr__(snapshot, "catalog_fingerprint", "e" * 64)
    assert not is_catalog_snapshot_issued(snapshot)
    with pytest.raises(CatalogTargetError, match="not issued or was modified"):
        snapshot.bind_target(1)


def test_binding_does_not_expose_public_practice_solution(tmp_path: Path) -> None:
    catalog = tmp_path / "puzzles.json"
    _write_catalog(catalog, (_puzzle(),))

    binding = load_snapshot(catalog).bind_target(1)

    assert "practice_solution" not in {field.name for field in dataclass_fields(binding)}
    assert "practice_solution" not in repr(binding)
    assert not hasattr(binding.target, "practice_solution")
    assert not hasattr(binding, "practice_solution")


def test_default_snapshot_ignores_generated_workspace_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_catalog = tmp_path / "data" / "puzzles.json"
    workspace_catalog.parent.mkdir()
    _write_catalog(workspace_catalog, (_puzzle(),))
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))

    packaged = load_snapshot()
    explicit_import = load_snapshot(workspace_catalog)

    assert len(packaged.entries) > 1
    assert tuple(entry.target.puzzle_id for entry in explicit_import.entries) == (1,)
    assert packaged.catalog_fingerprint != explicit_import.catalog_fingerprint
