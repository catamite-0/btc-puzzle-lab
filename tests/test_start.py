from btc_puzzle_lab.cli import main
from btc_puzzle_lab.relay import generate_relay_keypair
from btc_puzzle_lab.start import (
    OperatorConfig,
    format_start_prep,
    load_operator_config,
    merge_operator_config,
    operator_errors,
    persist_operator_config,
    prepare_start,
    upsert_env_file,
)
from btc_puzzle_lab.strategy import HostProfile
from btc_puzzle_lab.toolchain import InstallResult, SelfCheckResult

_DEST = "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
_NOTIFY = "https://example.com/hook"


def _cfg(**kwargs) -> OperatorConfig:
    payload = dict(
        dest_addr=_DEST,
        webhook_url=_NOTIFY,
        puzzle_id=1,
    )
    payload.update(kwargs)
    return OperatorConfig(**payload)


def _gpu_host() -> HostProfile:
    return HostProfile(
        cpus=8,
        mem_mb=32_768,
        engines=frozenset(),
        gpu=True,
        gpu_name="RTX 5090",
        tier="gpu",
    )


def test_config_persists_dest_and_notify(capsys):
    code = main(["config", "--dest", _DEST, "--notify", _NOTIFY])
    assert code == 0
    out = capsys.readouterr().out
    assert _DEST in out
    assert "webhook" in out
    loaded = load_operator_config()
    assert loaded.dest_addr == _DEST
    assert loaded.webhook_url == _NOTIFY


def test_config_can_be_filled_in_two_steps(capsys):
    assert main(["config", "--dest", _DEST]) == 0
    out = capsys.readouterr().out
    assert "still needed" in out
    assert main(["config", "--notify", _NOTIFY]) == 0
    loaded = load_operator_config()
    assert loaded.dest_addr == _DEST
    assert loaded.notify_configured is True


def test_start_without_config_explains_the_three_knobs(capsys):
    code = main(["start", "71"])
    assert code == 2
    err = capsys.readouterr().err
    assert "config --dest" in err
    assert "start 71" in err


def test_upsert_preserves_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keep me\nFOO=bar\nDEST=old\n", encoding="utf-8")
    upsert_env_file(path, {"DEST": "new"})
    text = path.read_text(encoding="utf-8")
    assert "# keep me" in text
    assert "FOO=bar" in text
    assert "DEST=new" in text
    assert "DEST=old" not in text


def test_prepare_local_engine_skips_install():
    prep = prepare_start(
        _cfg(puzzle_id=1),
        sync=False,
        install=True,
        selfcheck=True,
        host=HostProfile(cpus=2, mem_mb=2048, engines=frozenset(), tier="standard"),
    )
    assert prep.blocker is None
    assert prep.plan.engine == "sequential"
    assert prep.installed == []
    assert "skipped (local engine" in format_start_prep(prep)


def test_cli_start_prepare_hunt_without_dest(capsys):
    _, pub = generate_relay_keypair()
    code = main(
        [
            "start",
            "1",
            "--relay",
            "https://127.0.0.1:8787/hit",
            "--relay-seal-pubkey",
            pub,
            "--relay-token",
            "control-hub-token-1",
            "--prepare-only",
            "--no-sync",
            "--no-install",
            "--no-selfcheck",
            "--no-doctor",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "method     : sequential" in out
    assert "hub sweep" in out
    assert "control-hub-token-1" not in out


def test_cli_start_prepare_only_practice(capsys):
    code = main(
        [
            "start",
            "1",
            "--dest",
            _DEST,
            "--notify",
            _NOTIFY,
            "--prepare-only",
            "--no-sync",
            "--no-install",
            "--no-selfcheck",
            "--no-doctor",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "method     : sequential" in out
    assert "puzzle     : #1" in out


def test_prepare_fetches_the_chosen_gpu_engine(monkeypatch, tmp_path):
    monkeypatch.setattr("btc_puzzle_lab.start.probe_host", lambda: _gpu_host())
    calls: list[list[str]] = []

    def fake_install(names, force=False):
        calls.append(list(names))
        path = tmp_path / "bin" / "cuBitCrack"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        path.chmod(0o755)
        monkeypatch.setenv("BITCRACK_PATH", str(path))
        return [InstallResult("bitcrack", True, path, "built")]

    monkeypatch.setattr("btc_puzzle_lab.start.install_engines", fake_install)
    monkeypatch.setattr(
        "btc_puzzle_lab.start.selfcheck_engines",
        lambda names, timeout=180.0: [
            SelfCheckResult(n, True, 20, "solved #20") for n in names
        ],
    )
    prep = prepare_start(_cfg(puzzle_id=71), sync=True, install=True, selfcheck=True)
    assert calls == [["bitcrack"]]
    assert prep.blocker is None
    assert prep.plan.engine == "bitcrack"
    assert prep.plan.resource == "gpu"
    assert prep.recommended_engine == "bitcrack"


def test_prepare_falls_back_when_gpu_solver_cannot_build(monkeypatch, tmp_path):
    monkeypatch.setattr("btc_puzzle_lab.start.probe_host", lambda: _gpu_host())

    def fake_install(names, force=False):
        name = names[0]
        if name == "bitcrack":
            return [InstallResult("bitcrack", False, None, "nvcc not found")]
        path = tmp_path / "bin" / "keyhunt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        path.chmod(0o755)
        monkeypatch.setenv("KEYHUNT_PATH", str(path))
        return [InstallResult("keyhunt", True, path, "built")]

    monkeypatch.setattr("btc_puzzle_lab.start.install_engines", fake_install)
    monkeypatch.setattr("btc_puzzle_lab.start.selfcheck_engines", lambda names, **kw: [])
    prep = prepare_start(_cfg(puzzle_id=71), sync=True, install=True, selfcheck=False)
    assert prep.blocker is None
    assert prep.plan.engine == "keyhunt"
    assert prep.plan.resource == "cpu"
    assert prep.recommended_engine == "bitcrack"
    assert "falling back" in prep.plan.reason


def test_live_start_requires_confirm_phrase(monkeypatch):
    monkeypatch.setenv("AUTO_TRANSFER_LIVE_CONFIRM", "nope")
    cfg = merge_operator_config(_cfg(), live=True)
    errors = operator_errors(cfg, require_puzzle=True)
    assert any("LIVE_CONFIRM" in item for item in errors)


def test_persist_remembers_last_puzzle():
    persist_operator_config(_cfg(puzzle_id=71))
    loaded = load_operator_config()
    assert loaded.puzzle_id == 71
    assert loaded.dest_addr == _DEST
