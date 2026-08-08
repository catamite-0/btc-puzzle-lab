from pathlib import Path
from unittest.mock import patch

from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.engines import (
    parse_privkey_text,
    resolve_binary,
    run_external_engine,
)


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
    puzzle = get_puzzle(40)
    fake = tmp_path / "cuBitCrack"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("BITCRACK_PATH", str(fake))
    result = run_external_engine(puzzle, "bitcrack")
    assert result.secret is None
    assert "--keyspace" in result.cmdline
    assert f"{puzzle.range_start:x}:{puzzle.range_end:x}" in result.cmdline
    assert puzzle.address in result.cmdline
    assert "-c" in result.cmdline
