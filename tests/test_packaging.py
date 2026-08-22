import re
import tomllib
from importlib.resources import files
from pathlib import Path

from btc_puzzle_lab import __version__
from btc_puzzle_lab.catalog import load_packaged_full_puzzles, load_puzzles
from btc_puzzle_lab.engines import ENGINES
from btc_puzzle_lab.paths import clear_path_cache, read_puzzles_json, workspace_root


def test_version_matches_pyproject():
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(__version__)}\]", changelog, flags=re.M)


def test_packaged_catalog_readable():
    text = files("btc_puzzle_lab").joinpath("data/puzzles.json").read_text(encoding="utf-8")
    assert '"puzzles"' in text
    assert read_puzzles_json()


def test_env_example_template_present():
    packaged = files("btc_puzzle_lab").joinpath("data/env.example").read_text(encoding="utf-8")
    assert "AUTO_TRANSFER_ENABLED" in packaged
    assert "KEYHUNT_PATH" in packaged


def test_env_example_copies_cannot_drift():
    """The checkout copy is what docs point at; the packaged one is what a wheel
    install gets from `config --write-example`. Nothing kept them in sync, so a
    knob documented in one could silently go missing from the other."""
    repo_root = Path(__file__).resolve().parents[1]
    checkout = (repo_root / "config" / ".env.example").read_text(encoding="utf-8")
    packaged = files("btc_puzzle_lab").joinpath("data/env.example").read_text(encoding="utf-8")
    assert checkout == packaged, "config/.env.example and data/env.example diverged"


def test_write_env_example_materialises_the_template(tmp_path, monkeypatch):
    from btc_puzzle_lab.settings import write_env_example

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()

    path, written = write_env_example()
    assert written and path.is_file()
    assert "AUTO_TRANSFER_DEST_ADDR" in path.read_text(encoding="utf-8")

    # A second call must not clobber a template the operator has annotated.
    path.write_text("# edited by hand\n", encoding="utf-8")
    _, written_again = write_env_example()
    assert not written_again
    assert path.read_text(encoding="utf-8") == "# edited by hand\n"

    _, forced = write_env_example(overwrite=True)
    assert forced
    assert "AUTO_TRANSFER_DEST_ADDR" in path.read_text(encoding="utf-8")


def test_workspace_home_override(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    assert workspace_root() == tmp_path.resolve()
    # Catalog still loads from package when workspace has no data/
    ids = {p.id for p in load_puzzles()}
    assert 1 in ids
    clear_path_cache()


def test_no_coinsense_hardcoded_paths():
    for spec in ENGINES.values():
        for candidate in spec.candidates:
            assert "coinsense" not in candidate


def test_packaged_catalog_is_practice_subset():
    # The legacy load_puzzles fallback stays the small practice set. Autopilot's
    # full loader independently reads the package CSV tested below.
    pkg_copy = files("btc_puzzle_lab").joinpath("data/puzzles.json").read_text(encoding="utf-8")
    assert '"id": 20' in pkg_copy
    ids = {p.id for p in load_puzzles()}
    assert {1, 20, 40, 50}.issubset(ids)


def test_packaged_full_catalog_is_available_without_workspace_import():
    puzzles = load_packaged_full_puzzles()

    assert len(puzzles) == 160
    assert sum(puzzle.status == "unsolved" for puzzle in puzzles) == 78
    assert {71, 140, 160}.issubset({puzzle.id for puzzle in puzzles})


def test_packaged_full_catalog_ignores_workspace_csv_override():
    workspace_csv = workspace_root() / "data" / "puzzle-tx-export.csv"
    workspace_csv.parent.mkdir(parents=True)
    workspace_csv.write_text("not,the,package,catalog\n", encoding="utf-8")

    puzzles = load_packaged_full_puzzles()

    assert len(puzzles) == 160
    assert {71, 140, 160}.issubset({puzzle.id for puzzle in puzzles})


def test_tests_never_run_against_the_real_checkout():
    # Guards tests/conftest.py: without the isolated workspace fixture, anything
    # that logs an event writes into the operator's live state/runs.jsonl.
    root = workspace_root()
    assert not (root / "pyproject.toml").is_file(), (
        f"tests are running against the checkout at {root}; "
        "the isolated_workspace fixture is not active"
    )
