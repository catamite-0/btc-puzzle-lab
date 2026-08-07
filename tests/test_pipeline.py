import json
from pathlib import Path

import btc_puzzle_lab.hits as hits_mod
import btc_puzzle_lab.runlog as runlog_mod
import btc_puzzle_lab.search as search_mod
from btc_puzzle_lab.audit import audit_hits, export_audit_report
from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.crypto import sequential_find_p2pkh
from btc_puzzle_lab.hits import read_hits
from btc_puzzle_lab.search import (
    ScanCheckpoint,
    load_checkpoint,
    run_inject_known,
    run_window,
    save_checkpoint,
)
from btc_puzzle_lab.summary import build_summary, format_summary


def test_sequential_finds_puzzle_20_in_small_window():
    puzzle = get_puzzle(20)
    assert puzzle.practice_solution is not None
    center = puzzle.practice_solution
    found = sequential_find_p2pkh(puzzle.address, center - 32, center + 32)
    assert found == center


def test_window_and_audit_pipeline(tmp_path: Path, monkeypatch):
    hits_file = tmp_path / "HITS.jsonl"
    runs_file = tmp_path / "runs.jsonl"
    monkeypatch.setattr(hits_mod, "HITS_FILE", hits_file)
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(search_mod, "append_hit", hits_mod.append_hit)
    monkeypatch.setattr(runlog_mod, "RUNS_FILE", runs_file)
    monkeypatch.setattr(runlog_mod, "ensure_state_dir", hits_mod.ensure_state_dir)

    puzzle = get_puzzle(40)
    outcome = run_window(puzzle, window=2048, progress=False)
    assert outcome.hit is not None
    assert outcome.hit.engine == "window"
    assert hits_file.exists()
    assert hits_file.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr("btc_puzzle_lab.audit.read_hits", hits_mod.read_hits)
    results = audit_hits(check_balance=False)
    assert len(results) == 1
    assert results[0].address_ok is True

    export_path = tmp_path / "audit.json"
    export_audit_report(results, export_path)
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["results"][0]["puzzle_id"] == 40
    assert "private_key" not in export_path.read_text(encoding="utf-8")


def test_hits_dedupe(tmp_path: Path, monkeypatch):
    hits_file = tmp_path / "HITS.jsonl"
    runs_file = tmp_path / "runs.jsonl"
    monkeypatch.setattr(hits_mod, "HITS_FILE", hits_file)
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(search_mod, "append_hit", hits_mod.append_hit)
    monkeypatch.setattr(runlog_mod, "RUNS_FILE", runs_file)
    monkeypatch.setattr(runlog_mod, "ensure_state_dir", hits_mod.ensure_state_dir)

    first = run_inject_known(get_puzzle(20))
    second = run_inject_known(get_puzzle(20))
    assert first.hit is not None
    assert second.duplicate is True
    assert len(read_hits(hits_file)) == 1


def test_checkpoint_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        search_mod, "scan_checkpoint_path", lambda pid: tmp_path / f"scan_{pid}.json"
    )
    ckpt = ScanCheckpoint(
        puzzle_id=16,
        engine="sequential",
        next_secret=0x9000,
        end=0xFFFF,
        updated_at="2026-01-01T00:00:00Z",
    )
    path = save_checkpoint(ckpt)
    loaded = load_checkpoint(16)
    assert loaded is not None
    assert loaded.next_secret == 0x9000
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600


def test_summary_includes_events(tmp_path: Path, monkeypatch):
    hits_file = tmp_path / "HITS.jsonl"
    runs_file = tmp_path / "runs.jsonl"
    monkeypatch.setattr(hits_mod, "HITS_FILE", hits_file)
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(search_mod, "append_hit", hits_mod.append_hit)
    monkeypatch.setattr(runlog_mod, "RUNS_FILE", runs_file)
    monkeypatch.setattr(runlog_mod, "ensure_state_dir", hits_mod.ensure_state_dir)
    monkeypatch.setattr("btc_puzzle_lab.summary.HITS_FILE", hits_file)
    monkeypatch.setattr("btc_puzzle_lab.summary.RUNS_FILE", runs_file)
    monkeypatch.setattr("btc_puzzle_lab.summary.STATE_DIR", tmp_path)
    monkeypatch.setattr("btc_puzzle_lab.summary.read_hits", hits_mod.read_hits)
    monkeypatch.setattr("btc_puzzle_lab.summary.read_events", runlog_mod.read_events)

    outcome = run_inject_known(get_puzzle(1))
    assert outcome.hit is not None
    summary = build_summary(recent=5, state_dir=tmp_path)
    text = format_summary(summary)
    assert summary.unique_hits >= 1
    assert "catalog puzzles" in text
    assert summary.hit_rows == 1
    assert summary.coverage_files == 0
    assert "coverage files" in text
