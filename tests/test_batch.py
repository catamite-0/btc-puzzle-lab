from btc_puzzle_lab import batch
from btc_puzzle_lab.batch import (
    build_plan,
    format_status,
    load_plan,
    prize_is_gone,
    run_batch,
    save_plan,
)
from btc_puzzle_lab.catalog import get_puzzle
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
    assert by_id[1].resource == "cpu"
    assert by_id[40].job_status == "ready"
    assert by_id[40].engine == "window"
    assert by_id[40].resource == "cpu"
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
    assert plan.jobs[0].engine == "keyhunt"
    assert plan.jobs[0].job_status == "blocked"
    assert "engines install" in (plan.jobs[0].blocker or "")


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


def _unsolved(address: str = "1QKBaU6WAeycb3DbKbLBkX7vJiaS8r42Xo"):
    from btc_puzzle_lab.catalog import Puzzle

    return Puzzle(
        id=140,
        bits=140,
        address=address,
        range_start=1 << 139,
        range_end=(1 << 140) - 1,
        pubkey_compressed_hex="03" + "1f" * 32,
        practice_solution=None,
        status="unsolved",
        engine_default="rckangaroo",
        notes="",
    )


def test_prize_check_blocks_a_swept_target(monkeypatch):
    # #135 was picked as the best GPU target while its 13.5 BTC had already been
    # swept; the catalog snapshot cannot see that, only the chain can.
    monkeypatch.setattr("btc_puzzle_lab.batch.fetch_balance_sats", lambda *a, **k: 0)
    batch._PRIZE_CACHE.clear()
    assert prize_is_gone(_unsolved()) is True


def test_prize_check_exempts_practice_puzzles(monkeypatch):
    # Practice entries were swept years ago; drilling against them is the point.
    monkeypatch.setattr("btc_puzzle_lab.batch.fetch_balance_sats", lambda *a, **k: 0)
    batch._PRIZE_CACHE.clear()
    assert prize_is_gone(get_puzzle(40)) is False


def test_prize_check_allows_a_funded_target(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.batch.fetch_balance_sats", lambda *a, **k: 1_400_000_000)
    batch._PRIZE_CACHE.clear()
    assert prize_is_gone(_unsolved()) is False


def test_prize_check_fails_open_when_the_explorer_is_down(monkeypatch):
    # A flaky explorer must never be able to stop the search.
    def boom(*a, **k):
        raise RuntimeError("explorer unreachable")

    monkeypatch.setattr("btc_puzzle_lab.batch.fetch_balance_sats", boom)
    batch._PRIZE_CACHE.clear()
    assert prize_is_gone(_unsolved()) is False


def test_prize_check_can_be_disabled(monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_SKIP_PRIZE_CHECK", "1")
    monkeypatch.setattr("btc_puzzle_lab.batch.fetch_balance_sats", lambda *a, **k: 0)
    batch._PRIZE_CACHE.clear()
    assert prize_is_gone(_unsolved()) is False
