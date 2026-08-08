from __future__ import annotations

import os
from functools import lru_cache
from importlib.resources import files
from pathlib import Path


def _is_checkout_root(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (
        path / "src" / "btc_puzzle_lab"
    ).is_dir()


@lru_cache(maxsize=1)
def workspace_root() -> Path:
    """Writable lab home for state/ and config/.

    Priority:
    1. BTC_PUZZLE_LAB_HOME
    2. git/editable checkout root (src layout)
    3. current working directory
    """
    env = os.environ.get("BTC_PUZZLE_LAB_HOME")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve().parent
    if here.name == "btc_puzzle_lab" and here.parent.name == "src":
        checkout = here.parents[1]
        if _is_checkout_root(checkout):
            return checkout
    return Path.cwd().resolve()


def clear_path_cache() -> None:
    """Test helper after changing BTC_PUZZLE_LAB_HOME or cwd."""
    workspace_root.cache_clear()


class _LazyPath:
    """Path-like proxy that resolves through workspace_root() on each use."""

    __slots__ = ("_resolver",)

    def __init__(self, resolver) -> None:
        self._resolver = resolver

    def _path(self) -> Path:
        return self._resolver()

    def __fspath__(self) -> str:
        return os.fspath(self._path())

    def __str__(self) -> str:
        return str(self._path())

    def __repr__(self) -> str:
        return repr(self._path())

    def __truediv__(self, other):
        return self._path() / other

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _LazyPath):
            return self._path() == other._path()
        return self._path() == other

    def __hash__(self) -> int:
        return hash(self._path())

    def __getattr__(self, name: str):
        return getattr(self._path(), name)


def read_puzzles_json() -> str:
    override = workspace_root() / "data" / "puzzles.json"
    if override.is_file():
        return override.read_text(encoding="utf-8")
    return files("btc_puzzle_lab").joinpath("data/puzzles.json").read_text(encoding="utf-8")


def puzzles_file() -> Path:
    """Workspace catalog override when present; else a non-authoritative label path."""
    override = workspace_root() / "data" / "puzzles.json"
    if override.is_file():
        return override
    # Packaged catalog is read via read_puzzles_json(); this path is informational.
    return workspace_root() / "data" / "puzzles.json"


def _env_example() -> Path:
    workspace_example = workspace_root() / "config" / ".env.example"
    if workspace_example.is_file():
        return workspace_example
    return workspace_root() / "config" / ".env.example"


REPO_ROOT = _LazyPath(workspace_root)
DATA_DIR = _LazyPath(lambda: workspace_root() / "data")
PUZZLES_FILE = _LazyPath(puzzles_file)
STATE_DIR = _LazyPath(lambda: workspace_root() / "state")
HITS_FILE = _LazyPath(lambda: workspace_root() / "state" / "HITS.jsonl")
RUNS_FILE = _LazyPath(lambda: workspace_root() / "state" / "runs.jsonl")
CONFIG_DIR = _LazyPath(lambda: workspace_root() / "config")
ENV_FILE = _LazyPath(lambda: workspace_root() / "config" / ".env")
ENV_EXAMPLE_FILE = _LazyPath(_env_example)


def scan_checkpoint_path(puzzle_id: int) -> Path:
    return STATE_DIR / f"scan_{puzzle_id}.json"


def coverage_path(puzzle_id: int) -> Path:
    return STATE_DIR / f"coverage_{puzzle_id}.json"
