from pathlib import Path

from btc_puzzle_lab.cli import main
from btc_puzzle_lab.engines import resolve_binary
from btc_puzzle_lab.paths import clear_path_cache
from btc_puzzle_lab.toolchain import (
    BITCRACK_REF,
    KANGAROO_REF,
    KEYHUNT_REF,
    InstallResult,
    _clone_or_update,
    _write_engines_env,
    build_gencode,
    format_install_results,
    install_engines,
)


def test_default_solver_refs_are_full_commit_hashes():
    for ref in (KEYHUNT_REF, KANGAROO_REF, BITCRACK_REF):
        assert len(ref) == 40
        assert all(char in "0123456789abcdef" for char in ref.lower())


def test_clone_or_update_fetches_and_checks_out_exact_ref(tmp_path, monkeypatch):
    dest = tmp_path / "vendor" / "solver"
    (dest / ".git").mkdir(parents=True)
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], *, cwd=None):
        commands.append(cmd)
        if cmd[-2:] == ["rev-parse", "HEAD"]:
            return 0, "abc123\n"
        return 0, ""

    monkeypatch.setattr("btc_puzzle_lab.toolchain._run", fake_run)
    _clone_or_update("https://example.com/solver.git", dest, "abc123")

    assert ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", "abc123"] in commands
    assert [
        "git",
        "-C",
        str(dest),
        "checkout",
        "--force",
        "--detach",
        "FETCH_HEAD",
    ] in commands


def test_clone_or_update_rejects_wrong_commit(tmp_path, monkeypatch):
    dest = tmp_path / "vendor" / "solver"
    (dest / ".git").mkdir(parents=True)
    expected = "a" * 40

    def fake_run(cmd: list[str], *, cwd=None):
        if cmd[-2:] == ["rev-parse", "HEAD"]:
            return 0, f"{'b' * 40}\n"
        return 0, ""

    monkeypatch.setattr("btc_puzzle_lab.toolchain._run", fake_run)
    try:
        _clone_or_update("https://example.com/solver.git", dest, expected)
        assert False, "expected source verification failure"
    except RuntimeError as exc:
        assert expected in str(exc)


def test_build_gencode_dual_sass_and_ptx():
    assert build_gencode("120") == (
        "-gencode arch=compute_120,code=sm_120 "
        "-gencode arch=compute_120,code=compute_120"
    )
    try:
        build_gencode("12.0")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_write_engines_env_and_resolve(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    fake = tmp_path / "bin" / "keyhunt"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    env_path = _write_engines_env({"keyhunt": fake.resolve()})
    assert env_path.is_file()
    assert "KEYHUNT_PATH=" in env_path.read_text(encoding="utf-8")
    monkeypatch.delenv("KEYHUNT_PATH", raising=False)
    assert resolve_binary("keyhunt") == fake.resolve()


def test_install_engines_reports_missing_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr(
        "btc_puzzle_lab.toolchain.missing_build_tools",
        lambda: ["g++"],
    )
    try:
        install_engines(["keyhunt"])
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "g++" in str(exc)


def test_install_engines_manual_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])
    results = install_engines(["rckangaroo"])
    assert len(results) == 1
    assert results[0].ok is False
    assert "RCKANGAROO_PATH" in results[0].message


def test_install_bitcrack_requires_cuda(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.cuda_available", lambda: False)
    results = install_engines(["bitcrack"])
    assert results[0].name == "bitcrack"
    assert results[0].ok is False
    assert "nvcc" in results[0].message


def test_install_engines_uses_stub_builders(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])

    def fake_keyhunt(*, force: bool = False):
        path = tmp_path / "bin" / "keyhunt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        path.chmod(0o755)
        return InstallResult("keyhunt", True, path.resolve(), "stub")

    monkeypatch.setattr("btc_puzzle_lab.toolchain.install_keyhunt", fake_keyhunt)
    results = install_engines(["keyhunt"])
    assert any(r.name == "keyhunt" and r.ok for r in results)
    assert any(r.name == "config" and r.ok for r in results)
    assert (tmp_path / "config" / "engines.env").is_file()


def test_format_install_results():
    text = format_install_results(
        [InstallResult("keyhunt", True, Path("/tmp/keyhunt"), "ok")]
    )
    assert "[ok] keyhunt" in text


def test_patch_kangaroo_sources_adds_cstdint(tmp_path):
    from btc_puzzle_lab.toolchain import _patch_kangaroo_sources

    header = tmp_path / "Timer.h"
    header.write_text("#include <string>\nuint32_t x;\n", encoding="utf-8")
    _patch_kangaroo_sources(tmp_path)
    assert "#include <cstdint>" in header.read_text(encoding="utf-8")


def test_cli_engines_status_and_install(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    assert main(["engines"]) == 0
    assert main(["engines", "status"]) == 0
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])
    monkeypatch.setattr(
        "btc_puzzle_lab.toolchain.install_keyhunt",
        lambda *, force=False: InstallResult(
            "keyhunt", True, tmp_path / "bin" / "keyhunt", "stub"
        ),
    )
    # Ensure config write has a real file path
    bin_path = tmp_path / "bin" / "keyhunt"
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("x", encoding="utf-8")
    bin_path.chmod(0o755)
    monkeypatch.setattr(
        "btc_puzzle_lab.toolchain.install_keyhunt",
        lambda *, force=False: InstallResult("keyhunt", True, bin_path.resolve(), "stub"),
    )
    assert main(["engines", "install", "--only", "keyhunt"]) == 0
