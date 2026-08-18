import os

from btc_puzzle_lab.autorun import format_auto_result, plan_file_for, run_auto
from btc_puzzle_lab.loop import LoopResult, WatchResult
from btc_puzzle_lab.strategy import HostProfile
from btc_puzzle_lab.toolchain import EnsureResult


def _host(*, gpu: bool = False, cpus: int = 8) -> HostProfile:
    return HostProfile(
        cpus=cpus,
        mem_mb=32_768,
        engines=frozenset(),
        gpu=gpu,
        gpu_name="RTX 5090" if gpu else "",
        tier="gpu" if gpu else "standard",
    )


def _stage(result, name):
    return next(s for s in result.stages if s.name == name)


def _stub_watch(monkeypatch, recorder=None):
    def fake(**kwargs):
        if recorder is not None:
            recorder.append(kwargs | {"env": dict(os.environ)})
        return WatchResult(
            passes=1,
            hits=0,
            stopped_reason="max_passes",
            last=LoopResult(
                host_tier="standard",
                resource=kwargs.get("resource", "cpu"),
                sync=None,
                plan_path=kwargs["plan_path"],
                selected_ids=[],
                batch=None,
            ),
        )

    monkeypatch.setattr("btc_puzzle_lab.autorun.run_watch", fake)


def _stub_toolchain(monkeypatch, calls=None, *, ok=True, message="built"):
    def fake(engine, **kwargs):
        if calls is not None:
            calls.append(engine)
        return EnsureResult(
            engine=engine,
            ok=ok,
            already_present=False,
            binary=None,
            message=message,
        )

    monkeypatch.setattr("btc_puzzle_lab.autorun.ensure_engine", fake)


def test_plan_only_stops_before_building_or_searching(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    builds: list[str] = []
    _stub_toolchain(monkeypatch, builds)

    result = run_auto(71, host=_host(), plan_only=True)

    assert result.ok
    assert [s.name for s in result.stages] == [
        "config",
        "catalog",
        "host",
        "engine",
        "target",
        "toolchain",
        "run",
    ]
    assert result.choice.engine == "keyhunt"
    assert result.watch is None
    assert "not started" in _stage(result, "run").detail
    # Nothing compiled. This used to provision the engine and only then report
    # that it had not searched, so asking which engine a GPU box would pick cost
    # a full nvcc build first.
    assert builds == []
    assert "would build" in _stage(result, "toolchain").detail


def test_unknown_puzzle_fails_at_the_catalog_stage():
    result = run_auto(9999, host=_host(), plan_only=True)
    assert not result.ok
    assert result.failed_stage == "catalog"
    assert "9999" in result.message


def test_blocked_engine_stops_before_any_build(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setattr("btc_puzzle_lab.toolchain.cuda_available", lambda: False)
    monkeypatch.setattr("btc_puzzle_lab.recommend.cuda_available", lambda: False)
    builds: list[str] = []
    _stub_toolchain(monkeypatch, builds)

    result = run_auto(140, host=_host(gpu=True), plan_only=True)

    assert not result.ok
    assert result.failed_stage == "engine"
    assert "CUDA" in result.message
    assert builds == []


def test_swept_target_stops_before_building(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: True)
    builds: list[str] = []
    _stub_toolchain(monkeypatch, builds)

    result = run_auto(71, host=_host(), plan_only=True)

    assert not result.ok
    assert result.failed_stage == "target"
    assert "already been claimed" in result.message
    assert builds == []


def test_ignore_swept_overrides_the_prize_check(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: True)
    _stub_toolchain(monkeypatch)
    result = run_auto(71, host=_host(), plan_only=True, ignore_swept=True)
    assert result.ok
    assert "skipped" in _stage(result, "target").detail


def test_solved_practice_target_skips_the_prize_check(monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("practice targets must not hit the explorer")

    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", explode)
    result = run_auto(16, host=_host(), plan_only=True)
    assert result.ok
    assert "practice target" in _stage(result, "target").detail


def test_toolchain_failure_stops_before_searching(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch, ok=False, message="build failed: no compiler")
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    result = run_auto(71, host=_host())

    assert not result.ok
    assert result.failed_stage == "toolchain"
    assert "no compiler" in result.message
    assert runs == []


def test_run_pins_engine_and_dp_then_restores_the_environment(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setattr("btc_puzzle_lab.recommend.cuda_available", lambda: True)
    monkeypatch.delenv("BTC_PUZZLE_LAB_ENGINE", raising=False)
    monkeypatch.delenv("BTC_PUZZLE_LAB_DP", raising=False)
    _stub_toolchain(monkeypatch)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    result = run_auto(140, host=_host(gpu=True), max_passes=1)

    assert result.ok
    assert result.choice.engine == "rckangaroo"
    call = runs[0]
    # The loop must not be free to re-derive a different engine mid-session.
    assert call["env"]["BTC_PUZZLE_LAB_ENGINE"] == "rckangaroo"
    assert call["env"]["BTC_PUZZLE_LAB_DP"] == "30"
    assert call["resource"] == "gpu"
    # An explicitly named id must survive the board's unsolved/bits-min screen.
    assert call["status"] == "all"
    assert call["bits_min"] is None
    assert call["puzzle_ids"] == [140]
    assert call["transfer"] is True
    # …and the pins must not leak into the rest of the process.
    assert "BTC_PUZZLE_LAB_ENGINE" not in os.environ
    assert "BTC_PUZZLE_LAB_DP" not in os.environ


def test_each_target_gets_its_own_job_board(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    run_auto(71, host=_host(), max_passes=1)

    assert runs[0]["plan_path"] == plan_file_for(71)
    assert plan_file_for(71) != plan_file_for(140)


def test_explicit_engine_override_wins(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    result = run_auto(71, host=_host(), plan_only=True, engine="kangaroo")
    assert result.choice.engine == "kangaroo"
    assert result.choice.resource == "cpu"
    assert "pinned by --engine" in result.choice.reason


def test_relay_hunt_does_not_sweep_locally(monkeypatch):
    from btc_puzzle_lab.relay import generate_relay_keypair

    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)
    _, pub = generate_relay_keypair()

    result = run_auto(
        71,
        host=_host(),
        max_passes=1,
        relay_url="https://control.example:8787/hit",
        relay_seal_pubkey=pub,
        relay_token="control-hub-token-1",
    )

    assert result.ok
    assert runs[0]["transfer"] is False
    assert "transfer=hub" in _stage(result, "run").detail


def test_auto_refuses_dest_plus_relay(monkeypatch):
    from btc_puzzle_lab.relay import generate_relay_keypair

    _, pub = generate_relay_keypair()
    result = run_auto(
        71,
        host=_host(),
        plan_only=True,
        dest_addr="1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
        relay_url="https://control.example:8787/hit",
        relay_seal_pubkey=pub,
        relay_token="control-hub-token-1",
    )
    assert not result.ok
    assert result.failed_stage == "config"
    assert "cannot both be set" in result.message


def test_formatted_report_numbers_every_stage(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    result = run_auto(71, host=_host(), plan_only=True)
    text = format_auto_result(result)
    assert "[1/7] config" in text
    assert "[4/7] engine" in text


def test_cli_auto_plan_only_reports_the_decision(tmp_path, monkeypatch, capsys):
    from btc_puzzle_lab.cli import main
    from btc_puzzle_lab.paths import clear_path_cache

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.autorun.probe_host", lambda: _host())

    code = main(
        [
            "auto",
            "71",
            "--dest",
            "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
            "--notify",
            "https://ntfy.sh/topic",
            "--plan-only",
            "--no-build",
            "--ignore-swept",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "[4/7] engine" in out
    assert "keyhunt" in out
    # A plain --dest configures a dry-run sweep, never a live broadcast.
    env_text = (tmp_path / "config" / ".env").read_text(encoding="utf-8")
    assert "AUTO_TRANSFER_DRY_RUN=true" in env_text
    assert "AUTO_TRANSFER_LIVE_CONFIRM" not in env_text


def test_cli_auto_rejects_a_bad_destination(tmp_path, monkeypatch, capsys):
    from btc_puzzle_lab.cli import main
    from btc_puzzle_lab.paths import clear_path_cache

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    code = main(["auto", "71", "--dest", "nonsense", "--plan-only"])
    assert code == 2
    assert "not a valid BTC address" in capsys.readouterr().out
