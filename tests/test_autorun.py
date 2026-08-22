import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from btc_puzzle_lab.autopilot.facts import GpuDevice, HostCapabilities
from btc_puzzle_lab.autorun import plan_file_for, run_auto
from btc_puzzle_lab.loop import LoopResult, WatchResult
from btc_puzzle_lab.toolchain import EnsureResult

GIB = 1024**3


def _capabilities(*, gpu: bool = False, cpus: int = 8, gpu_count: int = 1) -> HostCapabilities:
    gpus = tuple(
        GpuDevice(
            device_id=f"GPU-exact-{index}",
            name=f"exact RTX 5090 #{index}",
            memory_bytes=32 * GIB,
            compute_capability=(12, 0),
            multiprocessor_count=170,
        )
        for index in range(gpu_count if gpu else 0)
    )
    return HostCapabilities(
        architecture="x86_64",
        cpu_count=cpus,
        memory_bytes=128 * GIB,
        disk_free_bytes=100 * GIB,
        gpus=gpus,
    )


def _auto(
    puzzle_id: int,
    *,
    gpu: bool = False,
    cpus: int = 8,
    gpu_count: int = 1,
    **kwargs,
):
    exact = _capabilities(gpu=gpu, cpus=cpus, gpu_count=gpu_count)
    with patch("btc_puzzle_lab.autorun.discover_host", return_value=exact):
        return run_auto(puzzle_id, **kwargs)


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


def _stub_toolchain(monkeypatch, calls=None, environments=None, *, ok=True, message="built"):
    def fake(engine, **kwargs):
        if calls is not None:
            calls.append(engine)
        if environments is not None:
            environments.append(dict(os.environ))
        return EnsureResult(
            engine=engine,
            ok=ok,
            already_present=False,
            binary=Path(f"/installed/{engine}"),
            message=message,
        )

    monkeypatch.setattr("btc_puzzle_lab.autorun.ensure_engine", fake)


def test_builtin_choice_runs_without_external_toolchain(monkeypatch):
    builds: list[str] = []
    _stub_toolchain(monkeypatch, builds)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    result = _auto(16, max_passes=1)

    assert result.ok
    assert result.choice.engine == "sequential"
    assert result.host.cpus == 7
    assert result.watch is not None
    assert builds == []
    assert "built in" in _stage(result, "toolchain").detail
    assert runs[0]["resource"] == "cpu"


def test_unknown_puzzle_fails_at_the_catalog_stage():
    result = _auto(9999)
    assert not result.ok
    assert result.failed_stage == "catalog"
    assert "9999" in result.message


def test_planner_reserves_one_cpu_for_selection_and_execution(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    builds: list[str] = []
    _stub_toolchain(monkeypatch, builds)

    result = _auto(71, cpus=1)

    assert not result.ok
    assert result.failed_stage == "engine"
    assert "no compatible algorithm" in result.message
    assert builds == []

    _stub_watch(monkeypatch)
    result = _auto(71, cpus=2, max_passes=1)
    assert result.ok
    assert result.host.cpus == 1


def test_swept_target_stops_before_building(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: True)
    builds: list[str] = []
    _stub_toolchain(monkeypatch, builds)

    result = _auto(71)

    assert not result.ok
    assert result.failed_stage == "target"
    assert "already been claimed" in result.message
    assert builds == []


def test_ignore_swept_overrides_the_prize_check(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: True)
    _stub_toolchain(monkeypatch)
    _stub_watch(monkeypatch)
    result = _auto(71, ignore_swept=True, max_passes=1)
    assert result.ok
    assert "skipped" in _stage(result, "target").detail


def test_solved_practice_target_skips_the_prize_check(monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("practice targets must not hit the explorer")

    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", explode)
    _stub_watch(monkeypatch)
    result = _auto(16, max_passes=1)
    assert result.ok
    assert "practice target" in _stage(result, "target").detail


def test_toolchain_failure_stops_before_searching(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch, ok=False, message="build failed: no compiler")
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    result = _auto(71)

    assert not result.ok
    assert result.failed_stage == "toolchain"
    assert "no compiler" in result.message
    assert runs == []


def test_host_discovery_failure_is_typed_and_redacted(monkeypatch):
    from btc_puzzle_lab.autopilot.host import HostDiscoveryCode, HostDiscoveryError

    secret = "nvidia-probe-secret-body"

    def fail():
        raise HostDiscoveryError(HostDiscoveryCode.NVIDIA_PROBE_FAILED, secret)

    monkeypatch.setattr("btc_puzzle_lab.autorun.discover_host", fail)

    result = run_auto(71)

    rendered = result.message + " ".join(stage.detail for stage in result.stages)
    assert not result.ok
    assert result.failed_stage == "host"
    assert "nvidia_probe_failed" in rendered
    assert secret not in rendered


def test_installed_gpu_engine_does_not_require_nvcc(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setattr(
        "btc_puzzle_lab.autorun.resolve_binary", lambda engine: Path(f"/installed/{engine}")
    )
    monkeypatch.setattr(
        "btc_puzzle_lab.autorun.cuda_available",
        lambda: (_ for _ in ()).throw(AssertionError("installed binary must not require nvcc")),
    )
    _stub_toolchain(monkeypatch)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    result = _auto(71, gpu=True, max_passes=1)

    assert result.ok
    assert (result.choice.engine, result.choice.resource) == ("bitcrack", "gpu")
    assert runs[0]["env"]["CUDA_VISIBLE_DEVICES"] == "GPU-exact-0"
    assert runs[0]["env"]["BTC_PUZZLE_LAB_GPU_INDEX"] == "0"
    assert runs[0]["env"]["BITCRACK_PATH"] == "/installed/bitcrack"


def test_missing_gpu_toolchain_can_explicitly_fall_back_to_cpu(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setattr("btc_puzzle_lab.autorun.resolve_binary", lambda _engine: None)
    monkeypatch.setattr("btc_puzzle_lab.autorun.cuda_available", lambda: False)
    builds: list[str] = []
    _stub_toolchain(monkeypatch, builds)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    result = _auto(71, gpu=True, allow_cpu_fallback=True, threads=1, max_passes=1)

    assert result.ok
    assert (result.choice.engine, result.choice.resource) == ("keyhunt", "cpu")
    assert builds == ["keyhunt"]
    assert runs[0]["resource"] == "cpu"
    assert runs[0]["env"]["BTC_PUZZLE_LAB_THREADS"] == "1"
    assert "CPU fallback" in _stage(result, "toolchain").detail


def test_nonbuilding_modes_reject_a_missing_binary(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setattr("btc_puzzle_lab.autorun.resolve_binary", lambda _engine: None)
    monkeypatch.setattr(
        "btc_puzzle_lab.autorun.ensure_engine",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not build")),
    )

    cases = (
        (_auto(71, build=False), "not installed"),
        (_auto(140, gpu=True, engine="rckangaroo"), "manual provisioning"),
    )
    for result, message in cases:
        assert not result.ok
        assert result.failed_stage == "toolchain"
        assert message in result.message


def test_no_build_and_manual_pins_use_verify_only(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setattr(
        "btc_puzzle_lab.autorun.resolve_binary", lambda engine: Path(f"/installed/{engine}")
    )
    calls: list[tuple[str, bool]] = []

    def verify(engine, **kwargs):
        calls.append((engine, kwargs["allow_build"]))
        return EnsureResult(
            engine=engine,
            ok=True,
            already_present=True,
            binary=Path(f"/installed/{engine}"),
            message="verified",
        )

    monkeypatch.setattr("btc_puzzle_lab.autorun.ensure_engine", verify)
    _stub_watch(monkeypatch)

    assert _auto(71, build=False, max_passes=1).ok
    assert _auto(140, gpu=True, engine="rckangaroo", max_passes=1).ok
    assert calls == [("keyhunt", False), ("rckangaroo", False)]


def test_multiple_visible_gpus_fail_before_preparation(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setattr(
        "btc_puzzle_lab.autorun.resolve_binary", lambda engine: Path(f"/installed/{engine}")
    )
    monkeypatch.setattr(
        "btc_puzzle_lab.autorun.ensure_engine",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not prepare")),
    )

    result = _auto(71, gpu=True, gpu_count=2)

    assert not result.ok
    assert result.failed_stage == "toolchain"
    assert "exactly one visible device" in result.message


def test_run_pins_engine_and_dp_then_restores_the_environment(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.delenv("BTC_PUZZLE_LAB_ENGINE", raising=False)
    monkeypatch.delenv("BTC_PUZZLE_LAB_DP", raising=False)
    range_knobs = {
        "BTC_PUZZLE_LAB_BITCRACK_RANDOM": "1",
        "BTC_PUZZLE_LAB_BITCRACK_CHUNK": "1000",
        "BTC_PUZZLE_LAB_RCKANGAROO_START": "8000",
        "BTC_PUZZLE_LAB_RCKANGAROO_RANGE_BITS": "32",
    }
    for name, value in range_knobs.items():
        monkeypatch.setenv(name, value)
    preparation_envs: list[dict] = []
    _stub_toolchain(monkeypatch, environments=preparation_envs)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    result = _auto(140, gpu=True, max_passes=1)

    assert result.ok
    assert result.choice.engine == "kangaroo"
    call = runs[0]
    # The loop must not be free to re-derive a different engine mid-session.
    assert call["env"]["BTC_PUZZLE_LAB_ENGINE"] == "kangaroo"
    assert call["env"]["BTC_PUZZLE_LAB_DP"] == "30"
    assert call["resource"] == "cpu"
    for snapshot in (preparation_envs[0], call["env"]):
        assert all(name not in snapshot for name in range_knobs)
    assert "BTC_PUZZLE_LAB_ENGINE" not in os.environ
    assert "BTC_PUZZLE_LAB_DP" not in os.environ
    assert all(os.environ[name] == value for name, value in range_knobs.items())


def test_each_target_gets_its_own_job_board(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    _auto(71, max_passes=1)

    assert runs[0]["plan_path"] == plan_file_for(71)
    assert plan_file_for(71) != plan_file_for(140)


def test_incompatible_explicit_engine_is_rejected_by_the_planner(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    result = _auto(71, engine="kangaroo")
    assert not result.ok
    assert result.failed_stage == "engine"
    assert result.choice.engine == "kangaroo"
    assert "PUBLIC_KEY_REQUIRED" in result.choice.blocked


def test_relay_hunt_does_not_sweep_locally(monkeypatch):
    from btc_puzzle_lab.relay import generate_relay_keypair

    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)
    _, pub = generate_relay_keypair()

    result = _auto(
        71,
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
        dest_addr="1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
        relay_url="https://control.example:8787/hit",
        relay_seal_pubkey=pub,
        relay_token="control-hub-token-1",
    )
    assert not result.ok
    assert result.failed_stage == "config"
    assert "cannot both be set" in result.message


def test_cli_auto_plan_only_reports_the_decision(tmp_path, monkeypatch, capsys):
    from btc_puzzle_lab.autopilot.pinned_plan import PinnedPlanOutcome
    from btc_puzzle_lab.cli import main
    from btc_puzzle_lab.paths import clear_path_cache

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    ports = object()
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.pinned_plan.production_pinned_plan_ports",
        lambda: ports,
    )

    def build(puzzle_id, *, ports):
        calls.append((puzzle_id, ports))
        return SimpleNamespace(
            outcome=PinnedPlanOutcome.SELECTED,
            render_text=lambda: "pinned plan v1: selected\nselected: engine=keyhunt",
        )

    monkeypatch.setattr("btc_puzzle_lab.autopilot.pinned_plan.build_pinned_plan", build)

    code = main(["auto", "71", "--plan"])
    out = capsys.readouterr().out
    assert code == 0
    assert calls == [(71, ports)]
    assert "pinned plan v1: selected" in out
    assert "keyhunt" in out
    for relative in ("config", "data", "state", "vendor", "bin"):
        assert not (tmp_path / relative).exists()


def test_cli_auto_plan_uses_distinct_blocked_exit_code(monkeypatch, capsys):
    from btc_puzzle_lab.autopilot.pinned_plan import PinnedPlanOutcome
    from btc_puzzle_lab.cli import main

    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.pinned_plan.production_pinned_plan_ports",
        lambda: object(),
    )
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.pinned_plan.build_pinned_plan",
        lambda *_args, **_kwargs: SimpleNamespace(
            outcome=PinnedPlanOutcome.BLOCKED,
            render_text=lambda: "pinned plan v1: blocked\nselected: none",
        ),
    )

    code = main(["auto", "71", "--plan-only"])

    assert code == 3
    assert "pinned plan v1: blocked" in capsys.readouterr().out


def test_cli_auto_plan_renders_typed_acquisition_error(monkeypatch, capsys):
    from btc_puzzle_lab.autopilot.pinned_plan import (
        PinnedPlanError,
        PinnedPlanErrorCode,
        PinnedPlanStage,
    )
    from btc_puzzle_lab.cli import main

    def fail_ports():
        raise PinnedPlanError(
            stage=PinnedPlanStage.CHAIN_COLLECTION,
            code=PinnedPlanErrorCode.CHAIN_COLLECTION_FAILED,
            detail="public chain quorum is unavailable",
            remedy="retry after both public providers recover",
        )

    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.pinned_plan.production_pinned_plan_ports",
        fail_ports,
    )

    code = main(["auto", "71", "--plan"])
    error = capsys.readouterr().err

    assert code == 2
    assert "chain_collection/chain_collection_failed" in error
    assert "retry after both public providers recover" in error


def test_cli_auto_catalog_plan_uses_fixed_production_preview(monkeypatch, capsys):
    from btc_puzzle_lab.autopilot.catalog_preview import CatalogPreviewOutcome
    from btc_puzzle_lab.cli import main

    class PortMarker:
        def __repr__(self) -> str:
            return "internal-authority-must-not-leak"

    ports = PortMarker()
    calls: list[object] = []
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.production_catalog_preview_ports",
        lambda: ports,
    )

    def build(*, ports):
        calls.append(ports)
        return SimpleNamespace(
            outcome=CatalogPreviewOutcome.SELECTED,
            authority="internal-authority-must-not-leak",
            render_text=lambda: (
                "catalog-preview outcome=selected\nselected rank=1 puzzle=#71 engine=keyhunt"
            ),
        )

    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.build_catalog_preview",
        build,
    )

    assert main(["auto", "--plan"]) == 0
    captured = capsys.readouterr()

    assert calls == [ports]
    assert captured.out.count("catalog-preview outcome=selected") == 1
    assert "internal-authority-must-not-leak" not in captured.out + captured.err


def test_cli_auto_catalog_plan_nonselected_outcomes_use_exit_three(monkeypatch, capsys):
    from btc_puzzle_lab.autopilot.catalog_preview import CatalogPreviewOutcome
    from btc_puzzle_lab.cli import main

    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.production_catalog_preview_ports",
        lambda: object(),
    )
    outcomes = (
        CatalogPreviewOutcome.INDETERMINATE,
        CatalogPreviewOutcome.NO_CONFIRMED_SELECTABLE_TARGET,
    )
    for outcome in outcomes:
        monkeypatch.setattr(
            "btc_puzzle_lab.autopilot.catalog_preview.build_catalog_preview",
            lambda *, ports, outcome=outcome: SimpleNamespace(
                outcome=outcome,
                render_text=lambda: f"catalog-preview outcome={outcome.value}",
            ),
        )
        assert main(["auto", "--plan"]) == 3

    output = capsys.readouterr().out
    assert "outcome=indeterminate" in output
    assert "outcome=no_confirmed_selectable_target" in output


def test_cli_auto_catalog_plan_renders_typed_static_error(monkeypatch, capsys):
    from btc_puzzle_lab.autopilot.catalog_preview import (
        CatalogPreviewError,
        CatalogPreviewErrorCode,
        CatalogPreviewStage,
    )
    from btc_puzzle_lab.cli import main

    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.production_catalog_preview_ports",
        lambda: object(),
    )

    def fail(*, ports):
        del ports
        raise CatalogPreviewError(
            stage=CatalogPreviewStage.CHAIN,
            code=CatalogPreviewErrorCode.CHAIN_COLLECTION_FAILED,
            detail="bounded public-chain collection failed",
            remedy="restore both public providers and retry",
        )

    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.build_catalog_preview",
        fail,
    )

    assert main(["auto", "--plan"]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "chain/chain_collection_failed" in captured.err
    assert "bounded public-chain collection failed" in captured.err
    assert "remedy: restore both public providers and retry" in captured.err


def test_cli_bare_auto_rejects_before_adapter_io_or_writes(tmp_path, monkeypatch, capsys):
    from btc_puzzle_lab.cli import main
    from btc_puzzle_lab.paths import clear_path_cache

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.production_catalog_preview_ports",
        lambda: (_ for _ in ()).throw(AssertionError("bare auto must not construct ports")),
    )

    assert main(["auto"]) == 2
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "requires a puzzle id unless --plan" in captured.err
    assert "Traceback" not in captured.err
    for relative in ("config", "data", "state", "vendor", "bin"):
        assert not (tmp_path / relative).exists()


def test_cli_plan_conflicts_do_not_construct_ports_echo_secrets_or_write(
    tmp_path, monkeypatch, capsys
):
    from btc_puzzle_lab.cli import main
    from btc_puzzle_lab.paths import clear_path_cache

    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.production_catalog_preview_ports",
        lambda: (_ for _ in ()).throw(AssertionError("conflicts must reject before ports")),
    )
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.pinned_plan.production_pinned_plan_ports",
        lambda: (_ for _ in ()).throw(AssertionError("conflicts must reject before ports")),
    )
    secrets = (
        "destination-secret",
        "https://hooks.example/catalog-secret",
        "catalog-telegram-secret",
        "catalog-chat-secret",
        "https://relay.example/catalog-secret",
        "catalog-seal-secret",
        "catalog-bearer-secret",
    )

    for target in ([], ["71"]):
        code = main(
            [
                "auto",
                *target,
                "--plan",
                "--dest",
                secrets[0],
                "--live",
                "--notify",
                secrets[1],
                "--telegram-token",
                secrets[2],
                "--telegram-chat",
                secrets[3],
                "--relay",
                secrets[4],
                "--relay-seal-pubkey",
                secrets[5],
                "--relay-token",
                secrets[6],
            ]
        )
        assert code == 2

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert "auto --plan is read-only" in rendered
    assert "--live" in rendered
    assert "authorises real BTC" not in rendered
    assert all(secret not in rendered for secret in secrets)
    for relative in ("config", "data", "state", "vendor", "bin"):
        assert not (tmp_path / relative).exists()


def test_cli_auto_without_plan_calls_the_runner(monkeypatch):
    from btc_puzzle_lab.cli import main

    def explode():
        raise AssertionError("normal auto must not enter the new plan-only adapters")

    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.pinned_plan.production_pinned_plan_ports",
        explode,
    )
    monkeypatch.setattr(
        "btc_puzzle_lab.autopilot.catalog_preview.production_catalog_preview_ports",
        explode,
    )
    calls: list[tuple[int, dict[str, object]]] = []

    def legacy(puzzle_id, **kwargs):
        calls.append((puzzle_id, kwargs))
        return SimpleNamespace(watch=None, message="", ok=True, failed_stage=None)

    monkeypatch.setattr("btc_puzzle_lab.autorun.run_auto", legacy)

    assert main(["auto", "71"]) == 0
    assert calls[0][0] == 71
    assert calls[0][1]["selfcheck_timeout"] == 180.0


def test_auto_honours_an_exported_engine_pin(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setenv("BTC_PUZZLE_LAB_ENGINE", "kangaroo")
    monkeypatch.setenv("BTC_PUZZLE_LAB_DP", "24")
    _stub_toolchain(monkeypatch)
    _stub_watch(monkeypatch)

    result = _auto(140, max_passes=1)

    assert result.choice.engine == "kangaroo"
    assert result.choice.dp == 24
    assert "BTC_PUZZLE_LAB_ENGINE" in result.choice.reason


def test_an_explicit_argument_outranks_the_environment(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    monkeypatch.setenv("BTC_PUZZLE_LAB_ENGINE", "rckangaroo")
    monkeypatch.setenv("BTC_PUZZLE_LAB_DP", "24")
    _stub_toolchain(monkeypatch)
    _stub_watch(monkeypatch)

    result = _auto(140, gpu=True, engine="kangaroo", dp=31, max_passes=1)

    assert result.choice.engine == "kangaroo"
    assert result.choice.dp == 31
    assert "--engine" in result.choice.reason


def test_invalid_engine_knobs_are_rejected_without_reaching_the_runner(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.autorun.prize_is_gone", lambda p, **k: False)
    _stub_toolchain(monkeypatch)
    runs: list[dict] = []
    _stub_watch(monkeypatch, runs)

    cases = (
        ("BTC_PUZZLE_LAB_DP", "not-an-int", "must be an integer"),
        ("BTC_PUZZLE_LAB_DP", "13", "between 14 and 32"),
        ("BTC_PUZZLE_LAB_THREADS", "0", "greater than zero"),
    )
    for name, value, message in cases:
        monkeypatch.setenv(name, value)
        result = _auto(140)
        assert not result.ok
        assert result.failed_stage == "engine"
        assert message in result.message
        monkeypatch.delenv(name)

    result = _auto(71, dp=30)
    assert not result.ok
    assert result.failed_stage == "engine"
    assert "applies only" in result.message

    result = _auto(140, cpus=2, threads=2)
    assert not result.ok
    assert "after reservation" in result.message
    assert runs == []
