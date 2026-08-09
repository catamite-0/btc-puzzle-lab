import re
import tomllib
from importlib.resources import files
from pathlib import Path

from btc_puzzle_lab import __version__
from btc_puzzle_lab.catalog import load_puzzles
from btc_puzzle_lab.engines import ENGINES
from btc_puzzle_lab.paths import clear_path_cache, read_puzzles_json, workspace_root


def test_version_matches_pyproject():
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(__version__)}\]", changelog, flags=re.M)


def test_project_urls_use_canonical_repository():
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    canonical = "https://github.com/catamite-0/btc-puzzle-lab"
    urls = data["project"]["urls"]
    assert urls["Homepage"] == canonical
    assert urls["Repository"] == canonical
    assert urls["Issues"] == f"{canonical}/issues"
    assert urls["Changelog"] == f"{canonical}/blob/main/CHANGELOG.md"

    for relative in ("README.md", "docs/MACHINE.md", "pyproject.toml"):
        text = (root / relative).read_text(encoding="utf-8")
        assert "catamitez0-maker/btc-puzzle-lab" not in text


def test_github_workflows_never_run_search_workloads():
    root = Path(__file__).resolve().parents[1]
    workflow_dir = root / ".github" / "workflows"
    workflows = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in sorted(workflow_dir.glob("*.yml"))
    )
    forbidden = (
        "machine-bootstrap",
        "engines install",
        "btc-puzzle-lab once",
        "btc-puzzle-lab watch",
        "--engine bitcrack",
        "nvidia-smi",
    )
    for command in forbidden:
        assert command not in workflows

    ci = (workflow_dir / "ci.yml").read_text(encoding="utf-8")
    assert "timeout-minutes: 15" in ci
    assert "contents: read" in ci
    assert 'BTC_PUZZLE_LAB_GPU: "0"' in ci


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


def test_packaged_catalog_is_practice_subset():
    # Packaged default stays the small practice set; full catalogs are imported
    # into workspace data/puzzles.json and intentionally may diverge.
    pkg_copy = files("btc_puzzle_lab").joinpath("data/puzzles.json").read_text(
        encoding="utf-8"
    )
    assert '"id": 20' in pkg_copy
    ids = {p.id for p in load_puzzles()}
    assert {1, 20, 40, 50}.issubset(ids)
