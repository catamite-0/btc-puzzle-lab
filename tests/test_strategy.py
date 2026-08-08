from btc_puzzle_lab.catalog import get_puzzle
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


def test_high_bits_prefer_keyhunt_when_present():
    plan = plan_strategy(get_puzzle(40), host=_host(engines={"keyhunt"}, cpus=4))
    assert plan.engine == "keyhunt"
    assert plan.threads == 4


def test_pubkey_prefers_rckangaroo_over_kangaroo_and_keyhunt():
    plan = plan_strategy(
        get_puzzle(40),
        host=_host(engines={"keyhunt", "kangaroo", "rckangaroo"}, cpus=4),
    )
    assert plan.engine == "rckangaroo"
    assert plan.dp == 16


def test_pubkey_falls_back_to_kangaroo():
    plan = plan_strategy(get_puzzle(40), host=_host(engines={"kangaroo", "keyhunt"}))
    assert plan.engine == "kangaroo"


def test_low_memory_caps_workers():
    plan = plan_strategy(get_puzzle(16), host=_host(cpus=4, mem_mb=1024))
    assert plan.workers == 1
