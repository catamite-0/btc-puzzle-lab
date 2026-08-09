from pathlib import Path
from unittest.mock import patch

from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.engines import (
    _solver_env,
    parse_privkey_text,
    redact_engine_line,
    resolve_binary,
    run_external_engine,
)
from btc_puzzle_lab.paths import clear_path_cache


def test_parse_privkey_text_common_formats():
    assert parse_privkey_text("PRIVATE KEY: 0000000000000000000000000000000000000000000000000000000000000015") == 0x15
    assert parse_privkey_text("Priv: 0xd2c55") == 0xD2C55
    assert parse_privkey_text("no key here") is None


def test_resolve_binary_respects_env(tmp_path: Path, monkeypatch):
    fake = tmp_path / "RCKangaroo"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    assert resolve_binary("rckangaroo") == fake


def test_run_external_missing_binary_is_clean():
    puzzle = get_puzzle(40)
    with patch("btc_puzzle_lab.engines.resolve_binary", return_value=None):
        result = run_external_engine(puzzle, "rckangaroo")
    assert result.secret is None
    assert "not found" in result.message
    assert "private" not in result.message.lower() or "no private key" in result.message


def test_run_external_parses_hit(tmp_path: Path, monkeypatch):
    puzzle = get_puzzle(5)
    fake = tmp_path / "keyhunt"
    fake.write_text("#!/bin/sh\necho 'Private key: 15'\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("KEYHUNT_PATH", str(fake))
    result = run_external_engine(puzzle, "keyhunt", threads=1)
    assert result.secret == 0x15
    assert result.engine == "keyhunt"


def test_rckangaroo_cmd_shape(tmp_path: Path, monkeypatch):
    puzzle = get_puzzle(40)
    fake = tmp_path / "RCKangaroo"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    result = run_external_engine(puzzle, "rckangaroo", dp=16)
    assert result.secret is None
    assert result.cmdline[0] == str(fake)
    assert "-pubkey" in result.cmdline
    assert puzzle.pubkey_compressed_hex in result.cmdline
    assert "-start" in result.cmdline
    assert f"{puzzle.range_start:x}" in result.cmdline


def test_bitcrack_cmd_shape(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    puzzle = get_puzzle(40)
    fake = tmp_path / "cuBitCrack"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("BITCRACK_PATH", str(fake))
    monkeypatch.setenv("BTC_PUZZLE_LAB_GPU_INDEX", "0")
    result = run_external_engine(puzzle, "bitcrack", progress=False)
    assert result.secret is None
    assert "--keyspace" in result.cmdline
    assert f"{puzzle.range_start:x}:{puzzle.range_end:x}" in result.cmdline
    assert puzzle.address in result.cmdline
    assert "-c" in result.cmdline
    assert result.cmdline[result.cmdline.index("-d") + 1] == "0"
    assert "--continue" in result.cmdline
    continue_file = Path(result.cmdline[result.cmdline.index("--continue") + 1])
    assert continue_file == tmp_path / "state" / "bitcrack_40.continue"


def test_redact_engine_line_hides_private_key():
    line = "Private key: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
    assert "0123456789abcdef" not in redact_engine_line(line)
    assert "[REDACTED]" in redact_engine_line(line)
    bare = "0123456789abcdef" * 4
    assert bare not in redact_engine_line(f"solver output {bare}\n")


def test_solver_environment_excludes_application_and_cloud_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("NOTIFY_TELEGRAM_BOT_TOKEN", "notify-secret")
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    env = _solver_env()
    assert env["PATH"] == "/usr/bin"
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
    assert "NOTIFY_TELEGRAM_BOT_TOKEN" not in env
    assert "RUNPOD_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_external_engine_timeout(tmp_path: Path, monkeypatch):
    puzzle = get_puzzle(5)
    fake = tmp_path / "keyhunt"
    fake.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("KEYHUNT_PATH", str(fake))
    result = run_external_engine(puzzle, "keyhunt", threads=1, timeout=0.5, progress=False)
    assert result.secret is None
    assert "timed out" in result.message
