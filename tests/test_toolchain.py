from pathlib import Path

from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.cli import main
from btc_puzzle_lab.engines import ExternalEngineResult, resolve_binary
from btc_puzzle_lab.paths import clear_path_cache
from btc_puzzle_lab.toolchain import (
    PINNED_COMMITS,
    SELFCHECK_PUZZLES,
    InstallResult,
    _write_engines_env,
    build_gencode,
    format_install_results,
    install_engines,
    selfcheck_engine,
)


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


def test_install_rckangaroo_requires_cuda(tmp_path, monkeypatch):
    # RCKangaroo used to be manual-only; it is installable now, but GPU-only.
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_headers", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.cuda_available", lambda: False)
    results = install_engines(["rckangaroo"])
    assert results[0].name == "rckangaroo"
    assert results[0].ok is False
    assert "nvcc" in results[0].message


def test_install_engines_reports_missing_headers(tmp_path, monkeypatch):
    # A host with git/make/g++ but no -dev packages used to pass this gate and
    # then fail deep inside `make`.
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_headers", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_headers", lambda: ["gmp.h"])
    try:
        install_engines(["keyhunt"])
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "gmp.h" in str(exc)
        assert "libgmp-dev" in str(exc)


def test_upstream_commits_are_pinned():
    # An unpinned clone means two hosts can silently get different solvers.
    for name, sha in PINNED_COMMITS.items():
        assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha), name


def test_install_bitcrack_requires_cuda(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_headers", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.cuda_available", lambda: False)
    results = install_engines(["bitcrack"])
    assert results[0].name == "bitcrack"
    assert results[0].ok is False
    assert "nvcc" in results[0].message


def test_install_engines_uses_stub_builders(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_tools", lambda: [])
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_headers", lambda: [])

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
    monkeypatch.setattr("btc_puzzle_lab.toolchain.missing_build_headers", lambda: [])
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
    # --no-selfcheck on purpose: the self-check executes real solvers, which must
    # never happen under pytest (CI runs pytest on GitHub-hosted runners).
    assert main(["engines", "install", "--only", "keyhunt", "--no-selfcheck"]) == 0


def test_cli_engines_selfcheck_reports_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setattr("btc_puzzle_lab.engines.resolve_binary", lambda name: Path("/bin/true"))
    monkeypatch.setattr(
        "btc_puzzle_lab.engines.run_external_engine",
        lambda puzzle, engine, **kw: ExternalEngineResult(engine, None, "stub"),
    )
    assert main(["engines", "selfcheck", "--only", "keyhunt"]) == 1
    assert "returned no key" in capsys.readouterr().out


def _stub_engine(monkeypatch, secret):
    monkeypatch.setattr("btc_puzzle_lab.engines.resolve_binary", lambda name: Path("/bin/true"))
    monkeypatch.setattr(
        "btc_puzzle_lab.engines.run_external_engine",
        lambda puzzle, engine, **kw: ExternalEngineResult(engine, secret, "stub"),
    )


def test_selfcheck_passes_when_engine_returns_the_known_key(monkeypatch):
    solution = get_puzzle(SELFCHECK_PUZZLES["keyhunt"]).practice_solution
    _stub_engine(monkeypatch, solution)
    result = selfcheck_engine("keyhunt")
    assert result.ok
    assert "solved" in result.message


def test_selfcheck_fails_when_engine_finds_nothing(monkeypatch):
    # The exact regression this exists for: the solver runs, exits clean, and the
    # lab never gets a key out of it.
    _stub_engine(monkeypatch, None)
    result = selfcheck_engine("keyhunt")
    assert not result.ok
    assert "returned no key" in result.message


def test_selfcheck_fails_on_wrong_key(monkeypatch):
    _stub_engine(monkeypatch, 0xDEAD)
    result = selfcheck_engine("bitcrack")
    assert not result.ok
    assert "wrong key" in result.message


def test_selfcheck_reports_uninstalled_engine(monkeypatch):
    monkeypatch.setattr("btc_puzzle_lab.engines.resolve_binary", lambda name: None)
    result = selfcheck_engine("rckangaroo")
    assert not result.ok
    assert result.message == "not installed"


def test_selfcheck_puzzles_are_solvable_by_their_engine():
    # kangaroo/rckangaroo need a pubkey; RCKangaroo also rejects -range below 32.
    for engine, pid in SELFCHECK_PUZZLES.items():
        puzzle = get_puzzle(pid)
        assert puzzle.practice_solution is not None, engine
        if engine in {"kangaroo", "rckangaroo"}:
            assert puzzle.pubkey_compressed_hex, engine
        if engine == "rckangaroo":
            assert puzzle.bits - 1 >= 32, engine
