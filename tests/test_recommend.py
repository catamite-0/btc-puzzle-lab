import pytest

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.recommend import SAFE_DP, cpu_alternative, recommend_engine
from btc_puzzle_lab.strategy import HostProfile


def _puzzle(bits: int, *, pubkey: str = "", solution: int | None = None) -> Puzzle:
    return Puzzle(
        id=bits,
        bits=bits,
        address="1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH",
        range_start=1 << (bits - 1),
        range_end=(1 << bits) - 1,
        pubkey_compressed_hex=pubkey,
        practice_solution=solution,
        status="unsolved" if solution is None else "solved",
        engine_default="window",
        notes="",
    )


def _host(*, gpu: bool = False, engines: set[str] | None = None, tier: str = "standard"):
    return HostProfile(
        cpus=8,
        mem_mb=32_768,
        engines=frozenset(engines or set()),
        gpu=gpu,
        gpu_name="RTX 5090" if gpu else "",
        tier="gpu" if gpu else tier,
    )


def test_tiny_range_needs_no_toolchain():
    choice = recommend_engine(_puzzle(16), _host(), cuda=False)
    assert choice.engine == "sequential"
    assert choice.resource == "cpu"
    assert not choice.needs_install
    assert choice.ok


def test_pubkey_on_gpu_picks_rckangaroo_with_a_survivable_dp():
    choice = recommend_engine(_puzzle(140, pubkey="02" + "ab" * 32), _host(gpu=True), cuda=True)
    assert (choice.engine, choice.resource) == ("rckangaroo", "gpu")
    assert choice.needs_install
    # dp=16 fills a 116 GB container in ~3.4h and loses the whole DP table with it.
    assert choice.dp == SAFE_DP >= 23


def test_pubkey_without_gpu_picks_cpu_kangaroo():
    choice = recommend_engine(_puzzle(140, pubkey="02" + "ab" * 32), _host(), cuda=False)
    assert (choice.engine, choice.resource) == ("kangaroo", "cpu")
    assert choice.dp == SAFE_DP


def test_address_only_targets_split_by_gpu():
    gpu = recommend_engine(_puzzle(71), _host(gpu=True), cuda=True)
    cpu = recommend_engine(_puzzle(71), _host(), cuda=False)
    assert (gpu.engine, gpu.resource) == ("bitcrack", "gpu")
    assert (cpu.engine, cpu.resource) == ("keyhunt", "cpu")


def test_gpu_without_cuda_is_blocked_not_silently_downgraded():
    choice = recommend_engine(_puzzle(140, pubkey="02" + "ab" * 32), _host(gpu=True), cuda=False)
    assert not choice.ok
    assert "CUDA" in choice.blocked
    assert "--allow-cpu-fallback" in choice.remedy
    assert "blocked" in choice.format()


def test_downgrade_happens_only_when_asked():
    choice = recommend_engine(
        _puzzle(140, pubkey="02" + "ab" * 32),
        _host(gpu=True),
        cuda=False,
        allow_cpu_fallback=True,
    )
    assert (choice.engine, choice.resource) == ("kangaroo", "cpu")
    assert choice.ok


@pytest.mark.parametrize(
    ("puzzle", "gpu", "cuda"),
    [
        (_puzzle(140, pubkey="02" + "ab" * 32), True, True),
        (_puzzle(140, pubkey="02" + "ab" * 32), False, False),
        (_puzzle(71), True, True),
        (_puzzle(71), False, False),
    ],
)
def test_choice_ignores_which_binaries_happen_to_be_installed(puzzle, gpu, cuda):
    """Installing a solver must not move a target to another resource class.

    docs/ARCHITECTURE.md §5: installing RCKangaroo silently relocated puzzle #160
    from the CPU queue to the GPU queue because the resource class was read off
    available_engines(). The recommendation must depend on the target and the
    hardware only.
    """
    bare = recommend_engine(puzzle, _host(gpu=gpu, engines=set()), cuda=cuda)
    stocked = recommend_engine(
        puzzle,
        _host(gpu=gpu, engines={"keyhunt", "kangaroo", "bitcrack", "rckangaroo"}),
        cuda=cuda,
    )
    assert (bare.engine, bare.resource) == (stocked.engine, stocked.resource)


def test_cpu_alternative_pairs_the_families():
    assert cpu_alternative("rckangaroo") == "kangaroo"
    assert cpu_alternative("bitcrack") == "keyhunt"
    assert cpu_alternative("keyhunt") is None
