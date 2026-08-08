from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.cli import main
from btc_puzzle_lab.strategy import (
    HostProfile,
    adapt_recommendations,
    classify_tier,
    format_host_profile,
    plan_strategy,
    probe_host,
)


def test_classify_tiers():
    assert (
        classify_tier(cpus=1, mem_mb=1024, gpu=False, engines=frozenset())
        == "constrained"
    )
    assert (
        classify_tier(cpus=2, mem_mb=2048, gpu=False, engines=frozenset()) == "standard"
    )
    assert (
        classify_tier(cpus=8, mem_mb=16384, gpu=False, engines=frozenset()) == "compute"
    )
    assert classify_tier(cpus=2, mem_mb=2048, gpu=True, engines=frozenset()) == "gpu"
    assert (
        classify_tier(cpus=2, mem_mb=1024, gpu=False, engines=frozenset({"bitcrack"}))
        == "gpu"
    )


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_CPUS", "3")
    monkeypatch.setenv("BTC_PUZZLE_LAB_MEM_MB", "4096")
    monkeypatch.setenv("BTC_PUZZLE_LAB_GPU", "0")
    profile = probe_host()
    assert profile.cpus == 3
    assert profile.mem_mb == 4096
    assert profile.gpu is False
    assert "BTC_PUZZLE_LAB_CPUS" in profile.overrides


def test_adaptive_knobs_change_with_tier():
    constrained = plan_strategy(
        get_puzzle(20),
        host=HostProfile(cpus=1, mem_mb=1024, engines=frozenset(), tier="constrained"),
    )
    compute = plan_strategy(
        get_puzzle(20),
        host=HostProfile(cpus=8, mem_mb=16384, engines=frozenset(), tier="compute"),
    )
    assert constrained.tier == "constrained"
    assert compute.tier == "compute"
    assert (constrained.max_chunks or 0) <= (compute.max_chunks or 0)
    assert constrained.workers <= compute.workers


def test_format_and_adapt_text():
    profile = HostProfile(
        cpus=2,
        mem_mb=2048,
        engines=frozenset(),
        gpu=False,
        tier="standard",
    )
    text = format_host_profile(profile)
    assert "tier           : standard" in text
    tips = adapt_recommendations(profile)
    assert any("KEYHUNT_PATH" in tip or "external solvers" in tip for tip in tips)


def test_cli_host_and_adapt():
    assert main(["host"]) == 0
    assert main(["adapt"]) == 0
