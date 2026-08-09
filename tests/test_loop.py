from btc_puzzle_lab.batch import build_plan
from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.cli import main
from btc_puzzle_lab.loop import resolve_resource_filter, run_once, select_ready_jobs
from btc_puzzle_lab.paths import clear_path_cache
from btc_puzzle_lab.strategy import HostProfile


def _gpu_host(*, engines: set[str] | frozenset[str] | None = None) -> HostProfile:
    return HostProfile(
        cpus=8,
        mem_mb=32_768,
        engines=frozenset(engines or {"bitcrack", "keyhunt"}),
        gpu=True,
        gpu_name="RTX 5090",
        tier="gpu",
    )


def test_resolve_resource_auto_prefers_gpu_on_gpu_host():
    assert resolve_resource_filter("auto", _gpu_host()) == "gpu"
    cpu = HostProfile(cpus=2, mem_mb=2048, engines=frozenset({"keyhunt"}))
    assert resolve_resource_filter("auto", cpu) == "cpu"


def test_select_ready_jobs_picks_lowest_bits_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    fake = tmp_path / "bin" / "cuBitCrack"
    fake.parent.mkdir(parents=True)
    fake.write_text("x", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("BITCRACK_PATH", str(fake))
    puzzles = [
        Puzzle(
            id=72,
            bits=72,
            address="1JTK7s9YVYywfm5XUH7RNhHJH1LshCaRFR",
            range_start=1 << 71,
            range_end=(1 << 72) - 1,
            pubkey_compressed_hex="",
            practice_solution=None,
            status="unsolved",
            engine_default="window",
            notes="",
        ),
        Puzzle(
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
        ),
    ]
    plan = build_plan(puzzles=puzzles, host=_gpu_host())
    assert all(job.resource == "gpu" for job in plan.jobs)
    selected = select_ready_jobs(plan, resource="gpu", limit=1)
    assert [job.puzzle_id for job in selected] == [71]


def test_run_once_practice_hit_audits_without_transfer_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr(
        "btc_puzzle_lab.loop.resolve_binary",
        lambda name: tmp_path / "bin" / name if name == "bitcrack" else None,
    )
    # Force CPU practice path: no GPU solvers required after we stub gpu check off.
    host = HostProfile(cpus=2, mem_mb=2048, engines=frozenset())
    result = run_once(
        sync=False,
        status="all",
        bits_min=None,
        puzzle_ids=[1],
        limit=1,
        resource="cpu",
        require_doctor=False,
        audit=True,
        transfer=True,
        progress=False,
        host=host,
    )
    assert result.selected_ids == [1]
    assert result.batch is not None
    assert result.batch.hits == 1
    assert len(result.audits) == 1
    assert result.audits[0].address_ok is True
    assert result.transfers
    assert result.transfers[0].status == "skipped"
    assert result.ok is True


def test_cli_once_idle_gpu_without_ready_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    # Packaged practice catalog has no unsolved gpu-ready jobs.
    monkeypatch.setattr(
        "btc_puzzle_lab.loop.resolve_binary",
        lambda name: tmp_path / "fake" if name in {"bitcrack", "rckangaroo"} else None,
    )
    host = _gpu_host()
    monkeypatch.setattr("btc_puzzle_lab.loop.probe_host", lambda: host)
    code = main(
        [
            "once",
            "--no-sync",
            "--status",
            "unsolved",
            "--resource",
            "gpu",
            "--no-doctor",
            "--no-progress",
        ]
    )
    assert code == 0
