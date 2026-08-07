from pathlib import Path

import btc_puzzle_lab.coverage as coverage_mod
import btc_puzzle_lab.hits as hits_mod
import btc_puzzle_lab.runlog as runlog_mod
import btc_puzzle_lab.search as search_mod
from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.coverage import (
    CoverageLedger,
    build_chunks,
    format_coverage,
    get_or_create_coverage,
    load_coverage,
)
from btc_puzzle_lab.search import run_sequential


def test_build_chunks_cover_range_exactly():
    chunks = build_chunks(10, 25, 8)
    assert [(c.start, c.end) for c in chunks] == [(10, 17), (18, 25)]
    assert sum(c.size for c in chunks) == 16


def test_plan_prefers_in_progress_and_random_is_seeded():
    ledger = CoverageLedger(
        puzzle_id=16,
        range_start=0x8000,
        range_end=0x8000 + 40 - 1,
        chunk_size=10,
    )
    ledger.mark(2, "in_progress")
    seq = [c.index for c in ledger.plan(order="sequential", max_chunks=3)]
    assert seq[0] == 2
    a = [c.index for c in ledger.plan(order="random", seed=7, max_chunks=4)]
    b = [c.index for c in ledger.plan(order="random", seed=7, max_chunks=4)]
    c = [c.index for c in ledger.plan(order="random", seed=8, max_chunks=4)]
    assert a == b
    assert a[0] == 2
    assert a != c


def test_coverage_persists_and_skips_done(tmp_path: Path, monkeypatch):
    hits_file = tmp_path / "HITS.jsonl"
    runs_file = tmp_path / "runs.jsonl"
    cov_path = tmp_path / "coverage_5.json"
    monkeypatch.setattr(hits_mod, "HITS_FILE", hits_file)
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(search_mod, "append_hit", hits_mod.append_hit)
    monkeypatch.setattr(runlog_mod, "RUNS_FILE", runs_file)
    monkeypatch.setattr(runlog_mod, "ensure_state_dir", hits_mod.ensure_state_dir)
    monkeypatch.setattr(coverage_mod, "coverage_path", lambda pid: cov_path)
    monkeypatch.setattr(search_mod, "get_or_create_coverage", coverage_mod.get_or_create_coverage)
    monkeypatch.setattr(search_mod, "save_coverage", coverage_mod.save_coverage)

    puzzle = get_puzzle(5)
    first = run_sequential(
        puzzle,
        coverage=True,
        chunk_size=4,
        order="sequential",
        max_chunks=1,
        progress=False,
    )
    # Puzzle #5 solution is 0x15; range 0x10-0x1f. First chunk 0x10-0x13 misses.
    assert first.hit is None
    assert first.chunks_scanned == 1
    ledger = load_coverage(5)
    assert ledger is not None
    assert ledger.counts()["done"] == 1
    assert ledger.counts()["pending"] == 3

    second = run_sequential(
        puzzle,
        coverage=True,
        chunk_size=4,
        order="sequential",
        max_chunks=1,
        progress=False,
    )
    # Second chunk 0x14-0x17 contains 0x15.
    assert second.hit is not None
    assert second.hit.engine == "sequential"
    assert load_coverage(5).counts()["hit"] == 1
    assert "private_key" not in cov_path.read_text(encoding="utf-8")


def test_random_coverage_can_find_with_seed(tmp_path: Path, monkeypatch):
    hits_file = tmp_path / "HITS.jsonl"
    runs_file = tmp_path / "runs.jsonl"
    cov_path = tmp_path / "coverage_5.json"
    monkeypatch.setattr(hits_mod, "HITS_FILE", hits_file)
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(search_mod, "append_hit", hits_mod.append_hit)
    monkeypatch.setattr(runlog_mod, "RUNS_FILE", runs_file)
    monkeypatch.setattr(runlog_mod, "ensure_state_dir", hits_mod.ensure_state_dir)
    monkeypatch.setattr(coverage_mod, "coverage_path", lambda pid: cov_path)
    monkeypatch.setattr(search_mod, "get_or_create_coverage", coverage_mod.get_or_create_coverage)
    monkeypatch.setattr(search_mod, "save_coverage", coverage_mod.save_coverage)

    puzzle = get_puzzle(5)
    outcome = run_sequential(
        puzzle,
        coverage=True,
        chunk_size=4,
        order="random",
        seed=1,
        max_chunks=4,
        progress=False,
    )
    assert outcome.hit is not None
    text = format_coverage(outcome.coverage)
    assert "puzzle #5" in text
    assert outcome.coverage.coverage_ratio > 0


def test_mismatch_resets_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hits_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(coverage_mod, "coverage_path", lambda pid: tmp_path / f"coverage_{pid}.json")
    first, created = get_or_create_coverage(
        10, range_start=0x200, range_end=0x3FF, chunk_size=64
    )
    assert created is True
    first.mark(0, "done")
    coverage_mod.save_coverage(first)
    second, reset = get_or_create_coverage(
        10, range_start=0x200, range_end=0x3FF, chunk_size=128
    )
    assert reset is True
    assert second.chunk_size == 128
    assert second.counts()["pending"] == len(second.chunks)
