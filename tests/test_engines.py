import time
from pathlib import Path
from unittest.mock import patch

import pytest

from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.engines import (
    _append_result_files,
    _run,
    bitcrack_keyspace,
    parse_privkey_text,
    redact_engine_line,
    resolve_binary,
    run_external_engine,
)


def test_parse_privkey_text_common_formats():
    assert (
        parse_privkey_text(
            "PRIVATE KEY: 0000000000000000000000000000000000000000000000000000000000000015"
        )
        == 0x15
    )
    assert parse_privkey_text("Priv: 0xd2c55") == 0xD2C55
    assert parse_privkey_text("no key here") is None


def test_parse_privkey_text_bitcrack_found_file():
    # BitCrack -o file: "<address> <privkey> <pubkey>", no label.
    addr = "1FRoHA9xewq7DjrZ1psWJVeTer8gHRqEvR"
    line = (
        f"{addr} "
        "00000000000000000000000000000000000000000000000000000000e9ae4933 "
        "0209c58240e50e3ba3f833c82655e8725c037a2294e14cf5d73a5df8d56159de69"
    )
    assert parse_privkey_text(line, expected_address=addr) == 0xE9AE4933
    # Progress noise must not be mistaken for a hit.
    assert parse_privkey_text("[Info] 70.0% 1.2 MK/s", expected_address=addr) is None


def test_bitcrack_row_needs_the_address_we_are_searching_for():
    # A stale or multi-target result file must not be read as our hit.
    line = (
        "1FRoHA9xewq7DjrZ1psWJVeTer8gHRqEvR "
        "00000000000000000000000000000000000000000000000000000000e9ae4933 "
        "0209c58240e50e3ba3f833c82655e8725c037a2294e14cf5d73a5df8d56159de69"
    )
    assert parse_privkey_text(line, expected_address="1HsMJxNiV7TLxmoF6uJNkydxPFDog4NQum") is None
    # Unlabelled rows stay locked until a caller says which address it wants.
    assert parse_privkey_text(line) is None
    # Labelled output is still parsed without one.
    assert parse_privkey_text("Private key: 0xd2c55") == 0xD2C55


def test_append_result_files_picks_up_keyhunt(tmp_path: Path):
    (tmp_path / "KEYFOUNDKEYFOUND.txt").write_text(
        "Private Key: d2c55\nAddress 1HsMJxNiV7TLxmoF6uJNkydxPFDog4NQum\n",
        encoding="utf-8",
    )
    assert parse_privkey_text(_append_result_files(tmp_path, "scanning...")) == 0xD2C55


def test_bitcrack_keyspace_sequential_by_default(monkeypatch):
    monkeypatch.delenv("BTC_PUZZLE_LAB_BITCRACK_RANDOM", raising=False)
    puzzle = get_puzzle(32)
    assert bitcrack_keyspace(puzzle) == f"{puzzle.range_start:x}:{puzzle.range_end:x}"


def test_bitcrack_keyspace_random_window_stays_in_range(monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_BITCRACK_RANDOM", "1")
    monkeypatch.setenv("BTC_PUZZLE_LAB_BITCRACK_CHUNK", "0x1000")
    puzzle = get_puzzle(32)
    seen = set()
    for _ in range(50):
        start_hex, count_hex = bitcrack_keyspace(puzzle).split(":+")
        start, count = int(start_hex, 16), int(count_hex, 16)
        assert count == 0x1000
        assert puzzle.range_start <= start
        assert start + count - 1 <= puzzle.range_end
        seen.add(start)
    assert len(seen) > 1, "random mode must not pin a single start"


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


def test_kangaroo_cmd_passes_dp(tmp_path: Path, monkeypatch):
    puzzle = get_puzzle(40)
    fake = tmp_path / "kangaroo"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("KANGAROO_PATH", str(fake))
    result = run_external_engine(puzzle, "kangaroo", threads=2, progress=False)
    assert result.cmdline[0] == str(fake)
    assert result.cmdline[result.cmdline.index("-d") + 1] == "30"


def test_kangaroo_dp_clamped_to_upstream_range(tmp_path: Path, monkeypatch):
    fake = tmp_path / "kangaroo"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("KANGAROO_PATH", str(fake))
    result = run_external_engine(get_puzzle(40), "kangaroo", dp=8, progress=False)
    assert result.cmdline[result.cmdline.index("-d") + 1] == "14"


def test_rckangaroo_default_dp_is_safe(tmp_path: Path, monkeypatch):
    fake = tmp_path / "RCKangaroo"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    result = run_external_engine(get_puzzle(40), "rckangaroo", progress=False)
    assert result.cmdline[result.cmdline.index("-dp") + 1] == "30"


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


def test_rckangaroo_custom_subrange(tmp_path: Path, monkeypatch):
    puzzle = get_puzzle(40)
    fake = tmp_path / "RCKangaroo"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    start = puzzle.range_start + (1 << 38)
    monkeypatch.setenv("BTC_PUZZLE_LAB_RCKANGAROO_START", f"{start:x}")
    monkeypatch.setenv("BTC_PUZZLE_LAB_RCKANGAROO_RANGE_BITS", "37")
    result = run_external_engine(puzzle, "rckangaroo", progress=False)
    assert result.cmdline[result.cmdline.index("-start") + 1] == f"{start:x}"
    assert result.cmdline[result.cmdline.index("-range") + 1] == "37"


def test_rckangaroo_custom_subrange_must_stay_inside_puzzle(tmp_path: Path, monkeypatch):
    puzzle = get_puzzle(40)
    fake = tmp_path / "RCKangaroo"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    monkeypatch.setenv("BTC_PUZZLE_LAB_RCKANGAROO_START", f"{puzzle.range_end:x}")
    monkeypatch.setenv("BTC_PUZZLE_LAB_RCKANGAROO_RANGE_BITS", "37")
    with pytest.raises(ValueError, match="inside the puzzle range"):
        run_external_engine(puzzle, "rckangaroo", progress=False)


def test_bitcrack_cmd_shape(tmp_path: Path, monkeypatch):
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


def test_rckangaroo_links_cubin_into_workdir(tmp_path: Path, monkeypatch):
    # Without the cubin beside it RCKangaroo spins at 0 MKeys/s instead of failing.
    fake = tmp_path / "RCKangaroo"
    fake.write_text("#!/bin/sh\nls *.cubin\n", encoding="utf-8")
    fake.chmod(0o755)
    (tmp_path / "kernel_sm120.cubin").write_text("stub", encoding="utf-8")
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    result = run_external_engine(get_puzzle(40), "rckangaroo", dp=16, progress=False)
    assert result.cmdline[result.cmdline.index("-dp") + 1] == "16"


def test_rckangaroo_dp_clamped_to_upstream_max(tmp_path: Path, monkeypatch):
    fake = tmp_path / "RCKangaroo"
    fake.write_text("#!/bin/sh\necho nope\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("RCKANGAROO_PATH", str(fake))
    result = run_external_engine(get_puzzle(40), "rckangaroo", dp=48, progress=False)
    assert result.cmdline[result.cmdline.index("-dp") + 1] == "32"


def test_redact_engine_line_hides_private_key():
    line = "Private key: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
    assert "0123456789abcdef" not in redact_engine_line(line)
    assert "[REDACTED]" in redact_engine_line(line)


def test_external_engine_timeout(tmp_path: Path, monkeypatch):
    puzzle = get_puzzle(5)
    fake = tmp_path / "keyhunt"
    fake.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("KEYHUNT_PATH", str(fake))
    result = run_external_engine(puzzle, "keyhunt", threads=1, timeout=0.5, progress=False)
    assert result.secret is None
    assert "timed out" in result.message


def test_engine_stops_as_soon_as_result_file_has_the_key(tmp_path, monkeypatch):
    # keyhunt does not exit after writing its hit, so waiting for the process to
    # end burns the whole timeout on an already-solved puzzle.
    fake = tmp_path / "keyhunt"
    fake.write_text(
        "#!/bin/sh\nprintf 'Private Key: 15\\n' > KEYFOUNDKEYFOUND.txt\nsleep 60\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("KEYHUNT_PATH", str(fake))
    started = time.monotonic()
    result = run_external_engine(get_puzzle(5), "keyhunt", threads=1, timeout=30, progress=False)
    elapsed = time.monotonic() - started
    assert result.secret == 0x15
    assert elapsed < 10, f"should not wait out the 30s budget, took {elapsed:.1f}s"


def test_carriage_return_progress_is_captured(tmp_path, monkeypatch):
    # RCKangaroo/BitCrack/Kangaroo refresh progress with \r and never emit \n.
    # A readline() based reader sees none of it, which is how a run can drop to
    # half speed unnoticed.
    fake = tmp_path / "keyhunt"
    fake.write_text(
        "#!/bin/sh\nprintf 'Speed: 17600 MKeys/s\\rSpeed: 8000 MKeys/s\\r'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("KEYHUNT_PATH", str(fake))
    code, output = _run(
        [str(fake)], cwd=tmp_path, timeout=15, progress=False, expected_address=None
    )
    assert "Speed: 17600 MKeys/s" in output
    assert "Speed: 8000 MKeys/s" in output


def test_a_noisy_labelled_line_cannot_mask_the_real_key():
    """The label regex has to be loose; the address check is what makes it safe.

    "add" is valid hex, so a line reading "priv add 5" parsed as a key and won,
    turning a genuinely solved puzzle into an address-mismatch crash further down.
    """
    from btc_puzzle_lab.crypto import privkey_bytes, privkey_to_p2pkh_address

    pk_hex = "00" * 31 + "02"
    address = privkey_to_p2pkh_address(privkey_bytes(pk_hex))
    text = f"priv add 5\nsome noise\nPrivate key: {pk_hex}\n"

    assert parse_privkey_text(text, expected_address=address) == 2
    # Without a target address there is nothing to check against, so the first
    # parse still wins — which is exactly why callers pass one.
    assert parse_privkey_text(text) == 0xADD


def test_a_key_for_a_different_address_is_not_reported_as_a_hit():
    from btc_puzzle_lab.crypto import privkey_bytes, privkey_to_p2pkh_address

    other = privkey_to_p2pkh_address(privkey_bytes("00" * 31 + "07"))
    text = "Private key: " + "00" * 31 + "02\n"
    assert parse_privkey_text(text, expected_address=other) is None
