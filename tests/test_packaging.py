from importlib.resources import files

from btc_puzzle_lab.catalog import load_puzzles
from btc_puzzle_lab.engines import ENGINES
from btc_puzzle_lab.paths import clear_path_cache, read_puzzles_json, workspace_root


def test_packaged_catalog_readable():
    text = files("btc_puzzle_lab").joinpath("data/puzzles.json").read_text(encoding="utf-8")
    assert '"puzzles"' in text
    assert read_puzzles_json()


def test_packaged_env_example_present():
    text = files("btc_puzzle_lab").joinpath("data/env.example").read_text(encoding="utf-8")
    assert "AUTO_TRANSFER_ENABLED" in text
    assert "KEYHUNT_PATH" in text


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
