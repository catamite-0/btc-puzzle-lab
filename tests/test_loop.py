from btc_puzzle_lab.batch import build_plan
from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.cli import main
from btc_puzzle_lab.loop import (
    resolve_resource_filter,
    run_once,
    run_watch,
    select_ready_jobs,
)
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


def test_auto_resource_keeps_big_cpu_hosts_on_the_cpu_slot():
    """tier "compute" is the high-CPU/no-GPU class, not a GPU host.

    classify_tier only returns "compute" when there is neither a card nor a GPU
    solver, so routing it to the gpu queue made `once --resource auto` abort with
    "no GPU solver is installed" on every large CPU box.
    """
    compute = HostProfile(cpus=64, mem_mb=116_000, engines=frozenset(), tier="compute")
    assert resolve_resource_filter("auto", compute) == "cpu"


def test_explicit_resource_is_never_second_guessed():
    for requested in ("cpu", "gpu", "any"):
        assert resolve_resource_filter(requested, _gpu_host()) == requested


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


def test_transfer_is_not_gated_behind_audit(tmp_path, monkeypatch):
    """--no-audit must not silently disable the sweep.

    They are separate CLI switches, but the sweep used to sit inside the `if audit:`
    block, so skipping verification also skipped the transfer with no output saying so.
    """
    from btc_puzzle_lab.transfer import TransferResult

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    swept: list[int] = []
    monkeypatch.setattr(
        "btc_puzzle_lab.loop.sweep_hit",
        lambda hit, **kw: (
            swept.append(hit.puzzle_id),
            TransferResult(status="dry_run", message="stub"),
        )[1],
    )
    host = HostProfile(cpus=2, mem_mb=2048, engines=frozenset())
    result = run_once(
        sync=False,
        status="all",
        bits_min=None,
        puzzle_ids=[1],
        limit=1,
        resource="cpu",
        require_doctor=False,
        audit=False,
        transfer=True,
        notify=False,
        progress=False,
        host=host,
    )
    assert result.batch is not None and result.batch.hits == 1
    assert result.audits == []
    assert swept == [1]
    assert [t.status for t in result.transfers] == ["dry_run"]


def test_failed_audit_still_blocks_the_sweep(tmp_path, monkeypatch):
    from btc_puzzle_lab.audit import AuditResult
    from btc_puzzle_lab.transfer import TransferResult

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    swept: list[int] = []
    monkeypatch.setattr(
        "btc_puzzle_lab.loop.verify_hit",
        lambda hit: AuditResult(
            hit=hit,
            address_ok=False,
            derived_address="",
            balance_sats=None,
            error="address mismatch",
        ),
    )
    monkeypatch.setattr(
        "btc_puzzle_lab.loop.sweep_hit",
        lambda hit, **kw: (
            swept.append(hit.puzzle_id),
            TransferResult(status="dry_run", message="stub"),
        )[1],
    )
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
        notify=False,
        progress=False,
        host=host,
    )
    assert swept == []
    assert result.transfers == []
    assert result.ok is False


def test_run_watch_stops_on_hit(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    host = HostProfile(cpus=2, mem_mb=2048, engines=frozenset())
    result = run_watch(
        sync=False,
        status="all",
        bits_min=None,
        puzzle_ids=[1],
        limit=1,
        resource="cpu",
        require_doctor=False,
        audit=True,
        transfer=False,
        progress=False,
        max_passes=3,
        idle_sleep=0.0,
        host=host,
    )
    assert result.stopped_reason == "hit"
    assert result.hits == 1
    assert result.passes == 1


def test_relay_posts_even_when_notify_is_off(tmp_path, monkeypatch):
    from btc_puzzle_lab.relay import RelayResult

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setenv("RELAY_URL", "https://control.example:8787/hit")
    monkeypatch.setenv("RELAY_SEAL_PUBKEY", "ab" * 32)
    monkeypatch.setenv("RELAY_TOKEN", "control-hub-token-1")
    posted: list[int] = []
    monkeypatch.setattr(
        "btc_puzzle_lab.loop.deliver_relay",
        lambda hit, **kw: (
            posted.append(hit.puzzle_id),
            RelayResult(True, "stub"),
        )[1],
    )
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
        transfer=False,
        notify=False,
        progress=False,
        host=host,
    )
    assert result.batch is not None and result.batch.hits == 1
    assert posted == [1]
    assert any(n.channel == "relay" for n in result.notifications)


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
