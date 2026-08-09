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
    workflow_paths = sorted(
        [*workflow_dir.rglob("*.yml"), *workflow_dir.rglob("*.yaml")]
    )
    workflows = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in workflow_paths
    )
    forbidden = (
        "machine-bootstrap",
        "benchmark-gpu",
        "engines install",
        "btc-puzzle-lab import-catalog",
        "btc-puzzle-lab plan",
        "btc-puzzle-lab batch",
        "btc-puzzle-lab run",
        "btc-puzzle-lab once",
        "btc-puzzle-lab watch",
        "--engine bitcrack",
        "cubitcrack",
        "keyhunt",
        "kangaroo",
        "nvidia-smi",
        "runpod",
        "self-hosted",
    )
    for command in forbidden:
        assert command not in workflows

    ci = (workflow_dir / "ci.yml").read_text(encoding="utf-8")
    assert "timeout-minutes: 15" in ci
    assert "contents: read" in ci
    assert 'BTC_PUZZLE_LAB_GPU: "0"' in ci
    release = (workflow_dir / "release.yml").read_text(encoding="utf-8")
    assert "timeout-minutes: 15" in release
    for path in workflow_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("uses:") and "./" not in stripped:
                assert re.search(r"@[0-9a-f]{40}(?:\s+#|$)", stripped)
    assert workflows.count("persist-credentials: false") >= 2


def test_public_runpod_guidance_never_names_a_real_or_unsolved_target():
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "README.md",
        root / "SECURITY.md",
        root / "AGENTS.md",
        root / "docs" / "MACHINE.md",
        root / "docs" / "LOOP.md",
        root / "scripts" / "machine-bootstrap.sh",
        root / "src" / "btc_puzzle_lab" / "cli.py",
        root / "src" / "btc_puzzle_lab" / "strategy.py",
    )
    text = "\n".join(path.read_text(encoding="utf-8").lower() for path in paths)
    forbidden = (
        "once --ids 71",
        "watch --ids 71",
        "run 71",
        "strategy 71",
        "--status unsolved",
    )
    for phrase in forbidden:
        assert phrase not in text
    assert "benchmark-gpu" in text
    assert "fresh" in text or "random" in text

    runpod_text = "\n".join(
        path.read_text(encoding="utf-8").lower()
        for path in (root / "docs" / "MACHINE.md", root / "scripts" / "machine-bootstrap.sh")
    )
    assert not re.search(
        r"btc-puzzle-lab\s+(?:run\s+\d+|(?:once|watch)\b[^\n]*--ids(?:=|\s+)\d+)",
        runpod_text,
    )
    assert not re.search(
        r"\b(?:1|3)[a-km-zA-HJ-NP-Z1-9]{25,34}\b|\bbc1[ac-hj-np-z02-9]{11,71}\b",
        text,
    )


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
