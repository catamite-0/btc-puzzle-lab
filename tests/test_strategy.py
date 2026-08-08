from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.strategy import HostProfile, plan_strategy


def _host(*, cpus: int = 2, mem_mb: int = 2048, has_keyhunt: bool = False) -> HostProfile:
    return HostProfile(cpus=cpus, mem_mb=mem_mb, has_keyhunt=has_keyhunt)


def test_tiny_puzzle_is_sequential():
    plan = plan_strategy(get_puzzle(1), host=_host())
    assert plan.engine == "sequential"
    assert plan.coverage is False


def test_mid_sequential_uses_coverage_when_range_large():
    plan = plan_strategy(get_puzzle(20), host=_host(mem_mb=2048))
    assert plan.engine == "sequential"
    assert plan.coverage is True
    assert plan.max_chunks == 4


def test_high_bits_prefer_window_without_keyhunt():
    plan = plan_strategy(get_puzzle(40), host=_host(has_keyhunt=False))
    assert plan.engine == "window"
    assert plan.coverage is True


def test_high_bits_prefer_keyhunt_when_present():
    plan = plan_strategy(get_puzzle(40), host=_host(has_keyhunt=True, cpus=4))
    assert plan.engine == "keyhunt"
    assert plan.threads == 4


def test_low_memory_caps_workers():
    plan = plan_strategy(get_puzzle(16), host=_host(cpus=4, mem_mb=1024))
    assert plan.workers == 1
