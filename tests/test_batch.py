from btc_puzzle_lab.batch import build_plan, format_status, load_plan, run_batch, save_plan
from btc_puzzle_lab.cli import main
from btc_puzzle_lab.paths import clear_path_cache
from btc_puzzle_lab.strategy import HostProfile


def test_build_plan_marks_local_ready_and_external_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    # Use packaged practice catalog (no workspace override).
    plan = build_plan(
        status="all",
        puzzle_ids=[1, 40],
        host=HostProfile(cpus=2, mem_mb=2048, engines=frozenset()),
    )
    by_id = {job.puzzle_id: job for job in plan.jobs}
    assert by_id[1].job_status == "ready"
    assert by_id[1].engine == "sequential"
    assert by_id[40].job_status == "ready"
    assert by_id[40].engine == "window"
    path = save_plan(plan, tmp_path / "state" / "batch_plan.json")
    loaded = load_plan(path)
    assert loaded is not None
    assert len(loaded.jobs) == 2


def test_build_plan_blocks_missing_external_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    from btc_puzzle_lab.catalog import Puzzle

    puzzle = Puzzle(
        id=71,
        bits=71,
        address="1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU",
        range_start=1 << 70,
        range_end=(1 << 71) - 1,
        pubkey_compressed_hex="",
        practice_solution=None,
        status="unsolved",
        engine_default="window",
        notes="",
    )
    plan = build_plan(
        puzzles=[puzzle],
        host=HostProfile(cpus=2, mem_mb=2048, engines=frozenset()),
    )
    assert len(plan.jobs) == 1
    assert plan.jobs[0].engine == "bitcrack"
    assert plan.jobs[0].job_status == "blocked"
    assert "BITCRACK_PATH" in (plan.jobs[0].blocker or "")


def test_run_batch_limit_and_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    plan = build_plan(
        puzzle_ids=[1, 5],
        host=HostProfile(cpus=2, mem_mb=2048, engines=frozenset()),
    )
    result = run_batch(
        plan,
        limit=1,
        resume=True,
        stop_on_hit=True,
        progress=False,
        plan_path=tmp_path / "state" / "batch_plan.json",
    )
    assert result.attempted == 1
    assert result.hits == 1
    assert result.stopped_early is True
    status = format_status(load_plan(tmp_path / "state" / "batch_plan.json"))
    assert "hit" in status


def test_cli_plan_batch_status(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    assert main(["plan", "--ids", "1,5", "--verbose"]) == 0
    assert main(["batch", "--limit", "1", "--stop-on-hit", "--no-progress"]) == 0
    assert main(["status"]) == 0
