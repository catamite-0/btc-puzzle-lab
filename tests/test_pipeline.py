from pathlib import Path

import btc_puzzle_lab.hits as hits_mod
import btc_puzzle_lab.search as search_mod
from btc_puzzle_lab.audit import audit_hits
from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.crypto import sequential_find_p2pkh
from btc_puzzle_lab.search import run_inject_known, run_window


def test_sequential_finds_puzzle_20_in_small_window():
    puzzle = get_puzzle(20)
    assert puzzle.practice_solution is not None
    center = puzzle.practice_solution
    found = sequential_find_p2pkh(puzzle.address, center - 32, center + 32)
    assert found == center


def test_window_and_audit_pipeline(tmp_path: Path, monkeypatch):
    hits_file = tmp_path / "HITS.jsonl"
    monkeypatch.setattr(hits_mod, "HITS_FILE", hits_file)
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(search_mod, "append_hit", hits_mod.append_hit)

    puzzle = get_puzzle(40)
    outcome = run_window(puzzle, window=2048)
    assert outcome.hit is not None
    assert hits_file.exists()
    assert hits_file.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr("btc_puzzle_lab.audit.read_hits", hits_mod.read_hits)
    results = audit_hits(check_balance=False)
    assert len(results) == 1
    assert results[0].address_ok is True


def test_inject_known(tmp_path: Path, monkeypatch):
    hits_file = tmp_path / "HITS.jsonl"
    monkeypatch.setattr(hits_mod, "HITS_FILE", hits_file)
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)

    outcome = run_inject_known(get_puzzle(20))
    assert outcome.hit is not None
    assert outcome.hit.engine == "inject-known"
