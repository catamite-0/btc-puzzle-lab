from pathlib import Path

from btc_puzzle_lab.catalog import get_puzzle, load_puzzles
from btc_puzzle_lab.catalog_import import import_catalog, import_catalog_from_csv_text
from btc_puzzle_lab.cli import main
from btc_puzzle_lab.paths import clear_path_cache

FIXTURE = Path(__file__).parent / "fixtures" / "puzzle_export_sample.csv"


def test_import_from_local_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    result = import_catalog(csv_path=FIXTURE, output=tmp_path / "data" / "puzzles.json")
    assert result.count == 3
    assert result.solved == 2
    assert result.unsolved == 1
    assert result.with_pubkey == 2
    assert result.with_solution == 2

    puzzles = load_puzzles(result.path)
    ids = {p.id for p in puzzles}
    assert ids == {1, 5, 71}
    p71 = get_puzzle(71, result.path)
    assert p71.practice_solution is None
    assert p71.status == "unsolved"
    assert p71.engine_default == "window"
    assert get_puzzle(1, result.path).practice_solution == 1


def test_import_no_solutions_flag(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    result = import_catalog_from_csv_text(
        text,
        output=tmp_path / "puzzles.json",
        include_solutions=False,
    )
    assert result.with_solution == 0
    for puzzle in load_puzzles(result.path):
        assert puzzle.practice_solution is None


def test_cli_import_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    code = main(
        [
            "import-catalog",
            "--from-csv",
            str(FIXTURE),
            "--output",
            str(tmp_path / "data" / "puzzles.json"),
        ]
    )
    assert code == 0
    assert main(["list"]) == 0
    assert main(["verify", "1"]) == 0


def test_import_bundled_full_export(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    # Copy bundled export into the temp workspace so default import finds it,
    # or rely on package data when workspace has no CSV.
    result = import_catalog(output=tmp_path / "data" / "puzzles.json")
    assert result.count == 160
    assert result.solved + result.unsolved == 160
    puzzles = load_puzzles(result.path)
    assert puzzles[0].id == 1
    assert puzzles[-1].id == 160
