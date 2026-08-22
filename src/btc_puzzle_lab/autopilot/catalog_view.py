"""Strict catalog-to-autopilot fact conversion.

The legacy catalog is intentionally a simple interchange record.  Autopilot
must not infer live/practice eligibility from loosely related fields throughout
the orchestration code, so this module performs that classification once and
returns an immutable :class:`PuzzleTarget` with no solution material attached.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path

from btc_puzzle_lab.catalog import Puzzle, load_packaged_full_puzzles, load_puzzles
from btc_puzzle_lab.crypto import match_privkey_address, privkey_bytes

from ._issuance import ProcessLocalIssuance
from .facts import DomainValidationError, KeyRange, PuzzleTarget, TargetMode


class CatalogTargetError(ValueError):
    """Catalog metadata is incomplete or internally contradictory."""


_PRACTICE_FIXTURE_FACTORY_TOKEN = object()
_CATALOG_SNAPSHOT_FACTORY_TOKEN = object()
_CUSTOM_CATALOG_SNAPSHOT_FACTORY_TOKEN = object()
_PACKAGED_CATALOG_SNAPSHOT_FACTORY_TOKEN = object()
_CATALOG_BINDING_FACTORY_TOKEN = object()


class CatalogSnapshotProvenance(StrEnum):
    """Locally assigned source route for one validated catalog snapshot."""

    DESCRIPTIVE_V1 = "descriptive_v1"
    CUSTOM_PATH_V1 = "custom_path_v1"
    PACKAGE_V1 = "package_v1"


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class PracticeFixtureEvidence:
    """Opaque proof that a public catalog solution was verified for a target."""

    target: PuzzleTarget
    fixture_fingerprint: str

    def __init__(
        self,
        *,
        target: PuzzleTarget,
        fixture_fingerprint: str,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _PRACTICE_FIXTURE_FACTORY_TOKEN:
            raise CatalogTargetError("practice evidence must come from catalog validation")
        if target.mode is not TargetMode.PRACTICE:
            raise CatalogTargetError("practice evidence requires a practice target")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "fixture_fingerprint", fixture_fingerprint)

    def matches(self, target: PuzzleTarget) -> bool:
        return (
            is_practice_fixture_evidence_issued(self)
            and type(target) is PuzzleTarget
            and self.target == target
        )


@dataclass(frozen=True, slots=True)
class CatalogTargetEntry:
    target: PuzzleTarget
    practice_fixture: PracticeFixtureEvidence | None

    def __post_init__(self) -> None:
        if type(self.target) is not PuzzleTarget:
            raise CatalogTargetError("catalog entry target must be a PuzzleTarget")
        expected = self.target.mode is TargetMode.PRACTICE
        if expected != (type(self.practice_fixture) is PracticeFixtureEvidence):
            raise CatalogTargetError("catalog entry practice evidence does not match target mode")
        if self.practice_fixture is not None and not self.practice_fixture.matches(self.target):
            raise CatalogTargetError("catalog entry practice evidence belongs to another target")


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class CatalogTargetBinding:
    """One target signed by validation of the complete catalog snapshot."""

    target: PuzzleTarget
    practice_fixture: PracticeFixtureEvidence | None
    catalog_fingerprint: str

    def __init__(
        self,
        *,
        target: PuzzleTarget,
        practice_fixture: PracticeFixtureEvidence | None,
        catalog_fingerprint: str,
        _factory_token: object | None = None,
    ) -> None:
        if _factory_token is not _CATALOG_BINDING_FACTORY_TOKEN:
            raise CatalogTargetError("target bindings must come from a catalog snapshot")
        if type(target) is not PuzzleTarget:
            raise CatalogTargetError("catalog binding target must be a PuzzleTarget")
        expected_fixture = target.mode is TargetMode.PRACTICE
        if expected_fixture != (type(practice_fixture) is PracticeFixtureEvidence):
            raise CatalogTargetError("catalog binding practice evidence does not match target mode")
        if practice_fixture is not None:
            if not is_practice_fixture_evidence_issued(practice_fixture):
                raise CatalogTargetError("catalog binding practice evidence was not issued")
            if practice_fixture.target != target:
                raise CatalogTargetError(
                    "catalog binding practice evidence belongs to another target"
                )
        if (
            type(catalog_fingerprint) is not str
            or len(catalog_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in catalog_fingerprint)
        ):
            raise CatalogTargetError("catalog fingerprint must be a SHA-256 digest")
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "practice_fixture", practice_fixture)
        object.__setattr__(self, "catalog_fingerprint", catalog_fingerprint)


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class CatalogSnapshot:
    """Canonical, immutable view of one completely validated catalog."""

    entries: tuple[CatalogTargetEntry, ...]
    catalog_fingerprint: str
    provenance: CatalogSnapshotProvenance

    def __init__(
        self,
        *,
        entries: tuple[CatalogTargetEntry, ...],
        catalog_fingerprint: str,
        provenance: CatalogSnapshotProvenance,
        _factory_token: object | None = None,
    ) -> None:
        expected_token = {
            CatalogSnapshotProvenance.DESCRIPTIVE_V1: _CATALOG_SNAPSHOT_FACTORY_TOKEN,
            CatalogSnapshotProvenance.CUSTOM_PATH_V1: _CUSTOM_CATALOG_SNAPSHOT_FACTORY_TOKEN,
            CatalogSnapshotProvenance.PACKAGE_V1: _PACKAGED_CATALOG_SNAPSHOT_FACTORY_TOKEN,
        }.get(provenance)
        if expected_token is None or _factory_token is not expected_token:
            raise CatalogTargetError("catalog snapshots must come from complete catalog loading")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "catalog_fingerprint", catalog_fingerprint)
        object.__setattr__(self, "provenance", provenance)

    @property
    def targets(self) -> tuple[PuzzleTarget, ...]:
        return tuple(entry.target for entry in self.entries)

    def bind_target(self, puzzle_id: int) -> CatalogTargetBinding:
        """Issue a binding for one target in this exact catalog snapshot."""

        if not is_catalog_snapshot_issued(self):
            raise CatalogTargetError("catalog snapshot was not issued or was modified")
        if type(puzzle_id) is not int or puzzle_id < 1:
            raise CatalogTargetError("puzzle id must be a positive integer")
        for entry in self.entries:
            if entry.target.puzzle_id == puzzle_id:
                binding = CatalogTargetBinding(
                    target=entry.target,
                    practice_fixture=entry.practice_fixture,
                    catalog_fingerprint=self.catalog_fingerprint,
                    _factory_token=_CATALOG_BINDING_FACTORY_TOKEN,
                )
                _CATALOG_BINDING_ISSUANCE.issue(binding)
                return binding
        raise CatalogTargetError(f"unknown puzzle #{puzzle_id} (not in active catalog)")


_PRACTICE_FIXTURE_ISSUANCE = ProcessLocalIssuance(PracticeFixtureEvidence)
_CATALOG_BINDING_ISSUANCE = ProcessLocalIssuance(CatalogTargetBinding)
_CATALOG_SNAPSHOT_ISSUANCE = ProcessLocalIssuance(CatalogSnapshot)
_PACKAGED_CATALOG_SNAPSHOT_ISSUANCE = ProcessLocalIssuance(CatalogSnapshot)


def is_practice_fixture_evidence_issued(value: object) -> bool:
    """Return whether this exact fixture was issued here and remains unchanged."""

    return _PRACTICE_FIXTURE_ISSUANCE.is_valid(value)


def is_catalog_target_binding_issued(value: object) -> bool:
    """Return whether this exact binding was issued here and remains unchanged."""

    return _CATALOG_BINDING_ISSUANCE.is_valid(value)


def is_catalog_snapshot_issued(value: object) -> bool:
    """Return whether this exact snapshot was issued here and remains unchanged."""

    return _CATALOG_SNAPSHOT_ISSUANCE.is_valid(value)


def is_packaged_catalog_snapshot_issued(value: object) -> bool:
    """Return whether package-owned loading issued this unchanged snapshot."""

    return (
        type(value) is CatalogSnapshot
        and value.provenance is CatalogSnapshotProvenance.PACKAGE_V1
        and _CATALOG_SNAPSHOT_ISSUANCE.is_valid(value)
        and _PACKAGED_CATALOG_SNAPSHOT_ISSUANCE.is_valid(value)
    )


def target_from_puzzle(puzzle: Puzzle) -> PuzzleTarget:
    """Classify one legacy catalog row as a strict live or practice target.

    Publicly solved rows are practice targets only when the catalog also carries
    the public solution and that scalar belongs to the declared range.  The
    scalar is used for validation here and is deliberately not copied into the
    returned fact.
    """

    if type(puzzle) is not Puzzle:
        raise CatalogTargetError("puzzle must be a catalog Puzzle")
    integer_fields = (
        puzzle.id,
        puzzle.bits,
        puzzle.range_start,
        puzzle.range_end,
    )
    if any(type(value) is not int for value in integer_fields) or (
        puzzle.practice_solution is not None and type(puzzle.practice_solution) is not int
    ):
        raise CatalogTargetError("catalog puzzle integer fields must be exact integers")
    text_fields = (
        puzzle.address,
        puzzle.pubkey_compressed_hex,
        puzzle.status,
        puzzle.engine_default,
        puzzle.notes,
    )
    if any(type(value) is not str for value in text_fields):
        raise CatalogTargetError("catalog puzzle text fields must be exact strings")

    status = puzzle.status.strip().lower() if isinstance(puzzle.status, str) else ""
    if status not in {"solved", "unsolved"}:
        raise CatalogTargetError(
            f"puzzle #{puzzle.id} has unsupported catalog status {puzzle.status!r}"
        )

    try:
        key_range = KeyRange(start=puzzle.range_start, end=puzzle.range_end)
    except DomainValidationError as exc:
        raise CatalogTargetError(f"puzzle #{puzzle.id} has an invalid key range: {exc}") from exc

    solution = puzzle.practice_solution
    if status == "unsolved" and solution is not None:
        raise CatalogTargetError(
            f"puzzle #{puzzle.id} is marked unsolved but carries a public solution"
        )
    if status == "solved":
        if solution is None:
            raise CatalogTargetError(
                f"puzzle #{puzzle.id} is solved but has no public practice fixture"
            )
        if not key_range.contains(solution):
            raise CatalogTargetError(
                f"puzzle #{puzzle.id} public practice solution is outside its key range"
            )
        try:
            match_privkey_address(privkey_bytes(solution), puzzle.address)
        except ValueError as exc:
            raise CatalogTargetError(
                f"puzzle #{puzzle.id} public practice solution does not match its address"
            ) from exc
        mode = TargetMode.PRACTICE
        fixture_id = f"package-catalog-v1:puzzle-{puzzle.id}"
    else:
        mode = TargetMode.LIVE
        fixture_id = None

    try:
        return PuzzleTarget(
            puzzle_id=puzzle.id,
            key_range=key_range,
            address=puzzle.address,
            mode=mode,
            bits_label=puzzle.bits,
            public_key_hex=puzzle.pubkey_compressed_hex or None,
            practice_fixture_id=fixture_id,
        )
    except DomainValidationError as exc:
        raise CatalogTargetError(f"puzzle #{puzzle.id} is invalid: {exc}") from exc


def entry_from_puzzle(puzzle: Puzzle) -> CatalogTargetEntry:
    """Convert one row and bind verified public-practice evidence when required."""

    target = target_from_puzzle(puzzle)
    evidence = None
    if target.mode is TargetMode.PRACTICE:
        assert puzzle.practice_solution is not None  # established by target_from_puzzle
        material = (
            f"practice-fixture-v1\0{target.puzzle_id}\0{target.address}\0"
            f"{target.key_range.start:x}\0{target.key_range.end:x}\0"
            f"{puzzle.practice_solution:064x}"
        ).encode("ascii")
        evidence = PracticeFixtureEvidence(
            target=target,
            fixture_fingerprint=hashlib.sha256(material).hexdigest(),
            _factory_token=_PRACTICE_FIXTURE_FACTORY_TOKEN,
        )
        _PRACTICE_FIXTURE_ISSUANCE.issue(evidence)
    return CatalogTargetEntry(target=target, practice_fixture=evidence)


def _catalog_fingerprint(puzzles: tuple[Puzzle, ...]) -> str:
    """Hash every semantic ``Puzzle`` field in canonical puzzle-id order."""

    field_names = tuple(field.name for field in fields(Puzzle))
    material = {
        "schema": "btc-puzzle-lab-catalog-v1",
        "fields": field_names,
        "puzzles": [
            {name: getattr(puzzle, name) for name in field_names}
            for puzzle in sorted(puzzles, key=lambda item: item.id)
        ],
    }
    encoded = json.dumps(
        material,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_from_puzzles(
    puzzles: tuple[Puzzle, ...],
    *,
    provenance: CatalogSnapshotProvenance,
    factory_token: object,
) -> CatalogSnapshot:
    if type(puzzles) is not tuple or any(type(puzzle) is not Puzzle for puzzle in puzzles):
        raise CatalogTargetError("catalog rows must be an exact tuple of Puzzle values")
    entries = tuple(entry_from_puzzle(puzzle) for puzzle in puzzles)
    puzzle_ids = tuple(entry.target.puzzle_id for entry in entries)
    if len(set(puzzle_ids)) != len(puzzle_ids):
        raise CatalogTargetError("catalog contains duplicate puzzle ids")

    ordered_entries = tuple(sorted(entries, key=lambda entry: entry.target.puzzle_id))
    snapshot = CatalogSnapshot(
        entries=ordered_entries,
        catalog_fingerprint=_catalog_fingerprint(puzzles),
        provenance=provenance,
        _factory_token=factory_token,
    )
    _CATALOG_SNAPSHOT_ISSUANCE.issue(snapshot)
    if provenance is CatalogSnapshotProvenance.PACKAGE_V1:
        _PACKAGED_CATALOG_SNAPSHOT_ISSUANCE.issue(snapshot)
    return snapshot


def snapshot_from_puzzles(puzzles: tuple[Puzzle, ...]) -> CatalogSnapshot:
    """Validate rows as a descriptive snapshot without package provenance.

    This descriptive factory is useful for tests and offline callers, but it
    cannot authorize production catalog-wide ranking. Product code obtains
    package provenance only through :func:`load_snapshot` with no path.
    """

    return _snapshot_from_puzzles(
        puzzles,
        provenance=CatalogSnapshotProvenance.DESCRIPTIVE_V1,
        factory_token=_CATALOG_SNAPSHOT_FACTORY_TOKEN,
    )


def load_snapshot(path: Path | None = None) -> CatalogSnapshot:
    """Load and fingerprint the full package catalog, or an explicit JSON path."""

    if path is None:
        return _snapshot_from_puzzles(
            tuple(load_packaged_full_puzzles()),
            provenance=CatalogSnapshotProvenance.PACKAGE_V1,
            factory_token=_PACKAGED_CATALOG_SNAPSHOT_FACTORY_TOKEN,
        )
    return _snapshot_from_puzzles(
        tuple(load_puzzles(path)),
        provenance=CatalogSnapshotProvenance.CUSTOM_PATH_V1,
        factory_token=_CUSTOM_CATALOG_SNAPSHOT_FACTORY_TOKEN,
    )


__all__ = [
    "CatalogSnapshot",
    "CatalogSnapshotProvenance",
    "CatalogTargetBinding",
    "CatalogTargetEntry",
    "CatalogTargetError",
    "PracticeFixtureEvidence",
    "entry_from_puzzle",
    "is_catalog_snapshot_issued",
    "is_catalog_target_binding_issued",
    "is_packaged_catalog_snapshot_issued",
    "is_practice_fixture_evidence_issued",
    "load_snapshot",
    "snapshot_from_puzzles",
    "target_from_puzzle",
]
