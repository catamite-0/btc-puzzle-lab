from btc_puzzle_lab.catalog import Puzzle, get_puzzle
from btc_puzzle_lab.strategy import HostProfile, plan_strategy


def _host(
    *,
    cpus: int = 2,
    mem_mb: int = 2048,
    engines: set[str] | frozenset[str] | None = None,
) -> HostProfile:
    return HostProfile(cpus=cpus, mem_mb=mem_mb, engines=frozenset(engines or ()))


def test_tiny_puzzle_is_sequential():
    plan = plan_strategy(get_puzzle(1), host=_host())
    assert plan.engine == "sequential"
    assert plan.coverage is False


def test_mid_sequential_uses_coverage_when_range_large():
    plan = plan_strategy(get_puzzle(20), host=_host(mem_mb=2048))
    assert plan.engine == "sequential"
    assert plan.coverage is True
    assert plan.max_chunks == 4


def test_high_bits_prefer_window_without_external():
    plan = plan_strategy(get_puzzle(40), host=_host())
    assert plan.engine == "window"
    assert plan.coverage is True


def test_high_bits_prefer_bitcrack_before_keyhunt():
    plan = plan_strategy(
        get_puzzle(40), host=_host(engines={"keyhunt", "bitcrack"}, cpus=4)
    )
    assert plan.engine == "bitcrack"


def test_high_bits_prefer_keyhunt_when_no_bitcrack():
    plan = plan_strategy(get_puzzle(40), host=_host(engines={"keyhunt"}, cpus=4))
    assert plan.engine == "keyhunt"
    assert plan.threads == 4


def test_pubkey_prefers_rckangaroo_over_bitcrack():
    plan = plan_strategy(
        get_puzzle(40),
        host=_host(engines={"bitcrack", "keyhunt", "kangaroo", "rckangaroo"}, cpus=4),
    )
    assert plan.engine == "rckangaroo"
    assert plan.dp == 16


def test_pubkey_falls_back_to_kangaroo():
    plan = plan_strategy(get_puzzle(40), host=_host(engines={"kangaroo", "keyhunt"}))
    assert plan.engine == "kangaroo"


def test_low_memory_caps_workers():
    plan = plan_strategy(get_puzzle(16), host=_host(cpus=4, mem_mb=1024))
    assert plan.workers == 1


def test_unsolved_without_engines_names_address_algorithm():
    puzzle = Puzzle(
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
    )
    plan = plan_strategy(puzzle, host=_host())
    assert plan.engine == "keyhunt"
    assert "KEYHUNT_PATH" in plan.reason


def test_unsolved_compute_tier_prefers_bitcrack_algorithm():
    puzzle = Puzzle(
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
    )
    plan = plan_strategy(
        puzzle, host=HostProfile(cpus=8, mem_mb=16384, engines=frozenset())
    )
    assert plan.engine == "bitcrack"
    assert "BITCRACK_PATH" in plan.reason


def test_unsolved_pubkey_without_engines_names_kangaroo_algorithm():
    puzzle = Puzzle(
        id=135,
        bits=135,
        address="16RGFo6hjq9ym6Pj7N5H7L1NR1rVPJyw2v",
        range_start=1 << 134,
        range_end=(1 << 135) - 1,
        pubkey_compressed_hex="02145d2611c823a396ef6712ce0f712f09b9b4f3135e3e0aa3230fb9b6d08d1e16",
        practice_solution=None,
        status="unsolved",
        engine_default="window",
        notes="",
    )
    plan = plan_strategy(puzzle, host=_host())
    assert plan.engine == "rckangaroo"
    assert "RCKANGAROO_PATH" in plan.reason
