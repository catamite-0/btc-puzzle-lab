from pathlib import Path

import base58
import pytest

from btc_puzzle_lab.benchmark import (
    MAX_BENCHMARK_SECONDS,
    MIN_BENCHMARK_SECONDS,
    SYNTHETIC_BITS,
    SYNTHETIC_PUZZLE_ID_BASE,
    SYNTHETIC_RANGE_END,
    SYNTHETIC_RANGE_START,
    format_synthetic_gpu_benchmark,
    parse_bitcrack_checkpoint,
    run_synthetic_gpu_benchmark,
    synthetic_bitcrack_target,
)
from btc_puzzle_lab.cli import build_parser
from btc_puzzle_lab.crypto import is_valid_btc_address
from btc_puzzle_lab.engines import ExternalEngineResult
from btc_puzzle_lab.paths import clear_path_cache


def _checkpoint_text(*, next_key: int, elapsed_ms: int) -> str:
    return "\n".join(
        (
            f"start={SYNTHETIC_RANGE_START:x}",
            f"next={next_key:x}",
            f"end={SYNTHETIC_RANGE_END:x}",
            "blocks=32",
            "threads=256",
            "points=32",
            "compression=compressed",
            "device=0",
            f"elapsed={elapsed_ms}",
            "stride=1",
        )
    )


def test_synthetic_target_uses_supplied_entropy_and_is_not_a_bitcoin_address():
    entropy = bytes(range(20))
    target = synthetic_bitcrack_target(entropy)
    assert target == synthetic_bitcrack_target(entropy)
    assert target[0] not in {"1", "3"}
    assert len(target) <= 34
    assert not is_valid_btc_address(target)
    with pytest.raises(ValueError):
        base58.b58decode_check(target)
    assert base58.b58decode_check(target[1:]) == entropy


def test_checkpoint_parser_accepts_pinned_bitcrack_format():
    checkpoint = parse_bitcrack_checkpoint(
        _checkpoint_text(next_key=SYNTHETIC_RANGE_START + 0x1000, elapsed_ms=60_000)
    )
    assert checkpoint.start == SYNTHETIC_RANGE_START
    assert checkpoint.next_key == SYNTHETIC_RANGE_START + 0x1000
    assert checkpoint.end == SYNTHETIC_RANGE_END
    assert checkpoint.elapsed_ms == 60_000


@pytest.mark.parametrize(
    "text",
    (
        "start=1\nnext=2\nend=3\nelapsed=60000",
        "start=xyz\nnext=2\nend=3\nelapsed=60000\nstride=1",
        "start=3\nnext=2\nend=4\nelapsed=60000\nstride=1",
        "start=1\nnext=2\nend=3\nelapsed=0\nstride=1",
        "start=1\nnext=2\nend=3\nelapsed=60000\nstride=2",
        "start=1\nstart=1\nnext=2\nend=3\nelapsed=60000\nstride=1",
    ),
)
def test_checkpoint_parser_rejects_malformed_or_unsafe_state(text):
    with pytest.raises(ValueError):
        parse_bitcrack_checkpoint(text)


def test_synthetic_benchmark_runs_two_bounded_resuming_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    calls = []

    monkeypatch.setattr("btc_puzzle_lab.benchmark.secrets.randbelow", lambda _: 1)
    monkeypatch.setattr(
        "btc_puzzle_lab.benchmark.secrets.token_bytes", lambda size: b"\x42" * size
    )

    def fake_runner(puzzle, engine, *, timeout, progress, display_command):
        calls.append((puzzle, engine, timeout, progress, display_command))
        checkpoint = tmp_path / "state" / f"bitcrack_{puzzle.id}.continue"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(
            _checkpoint_text(
                next_key=SYNTHETIC_RANGE_START + len(calls) * 0x100000,
                elapsed_ms=len(calls) * 60_000,
            ),
            encoding="utf-8",
        )
        return ExternalEngineResult(engine, None, "bounded timeout")

    result = run_synthetic_gpu_benchmark(
        seconds=MIN_BENCHMARK_SECONDS,
        progress=False,
        runner=fake_runner,
    )

    assert len(calls) == 2
    assert all(
        call[1:] == ("bitcrack", MIN_BENCHMARK_SECONDS, False, False)
        for call in calls
    )
    puzzle = calls[0][0]
    assert puzzle.id == SYNTHETIC_PUZZLE_ID_BASE + 1
    assert puzzle.bits == SYNTHETIC_BITS
    assert puzzle.range_start == SYNTHETIC_RANGE_START
    assert puzzle.range_end == SYNTHETIC_RANGE_END
    assert puzzle.address == synthetic_bitcrack_target(b"\x42" * 20)
    assert puzzle.practice_solution is None
    assert result.rounds[0].advanced_keys > 0
    assert result.rounds[1].advanced_keys > 0
    assert result.grid == (32, 256, 32, "compressed", 0)
    assert puzzle.address not in format_synthetic_gpu_benchmark(result)
    assert not (tmp_path / "state" / "HITS.jsonl").exists()


@pytest.mark.parametrize(
    "seconds",
    (MIN_BENCHMARK_SECONDS - 0.01, MAX_BENCHMARK_SECONDS + 0.01),
)
def test_synthetic_benchmark_rejects_unbounded_duration(seconds):
    with pytest.raises(ValueError, match="between"):
        run_synthetic_gpu_benchmark(seconds=seconds, runner=lambda *args, **kwargs: None)


@pytest.mark.parametrize("flag", ("--address", "--keyspace", "--ids", "--puzzle"))
def test_synthetic_benchmark_cli_has_no_target_override(flag):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["benchmark-gpu", flag, "anything"])


def test_synthetic_benchmark_discards_key_material_returned_by_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()

    def fake_runner(puzzle, engine, *, timeout, progress, display_command):
        checkpoint = Path(tmp_path) / "state" / f"bitcrack_{puzzle.id}.continue"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.write_text(
            _checkpoint_text(
                next_key=SYNTHETIC_RANGE_START + 0x100000,
                elapsed_ms=60_000,
            ),
            encoding="utf-8",
        )
        return ExternalEngineResult(engine, 0xDEADBEEF, "unexpected")

    with pytest.raises(RuntimeError, match="result discarded") as error:
        run_synthetic_gpu_benchmark(
            seconds=MIN_BENCHMARK_SECONDS,
            progress=False,
            runner=fake_runner,
        )
    assert "deadbeef" not in str(error.value).lower()


def test_synthetic_benchmark_rejects_hits_file_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()

    def fake_runner(puzzle, engine, *, timeout, progress, display_command):
        state = Path(tmp_path) / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / f"bitcrack_{puzzle.id}.continue").write_text(
            _checkpoint_text(
                next_key=SYNTHETIC_RANGE_START + 0x100000,
                elapsed_ms=60_000,
            ),
            encoding="utf-8",
        )
        (state / "HITS.jsonl").write_text("must not be written\n", encoding="utf-8")
        return ExternalEngineResult(engine, None, "bounded timeout")

    with pytest.raises(RuntimeError, match="must not create or modify"):
        run_synthetic_gpu_benchmark(
            seconds=MIN_BENCHMARK_SECONDS,
            progress=False,
            runner=fake_runner,
        )


def test_synthetic_benchmark_uses_a_fresh_checkpoint_id(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    state = tmp_path / "state"
    state.mkdir()
    occupied = SYNTHETIC_PUZZLE_ID_BASE + 1
    (state / f"bitcrack_{occupied}.continue").write_text(
        _checkpoint_text(next_key=SYNTHETIC_RANGE_START + 1, elapsed_ms=60_000),
        encoding="utf-8",
    )
    values = iter((1, 2))
    monkeypatch.setattr("btc_puzzle_lab.benchmark.secrets.randbelow", lambda _: next(values))

    calls = []

    def fake_runner(puzzle, engine, *, timeout, progress, display_command):
        calls.append(puzzle.id)
        checkpoint = state / f"bitcrack_{puzzle.id}.continue"
        checkpoint.write_text(
            _checkpoint_text(
                next_key=SYNTHETIC_RANGE_START + len(calls) * 0x100000,
                elapsed_ms=len(calls) * 60_000,
            ),
            encoding="utf-8",
        )
        return ExternalEngineResult(engine, None, "bounded timeout")

    run_synthetic_gpu_benchmark(
        seconds=MIN_BENCHMARK_SECONDS,
        progress=False,
        runner=fake_runner,
    )
    assert calls == [SYNTHETIC_PUZZLE_ID_BASE + 2] * 2
