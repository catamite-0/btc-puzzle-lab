import pytest

from btc_puzzle_lab.autopilot.facts import GpuDevice, HostCapabilities
from btc_puzzle_lab.autopilot.planning import ProvisioningPolicy
from btc_puzzle_lab.catalog import load_packaged_full_puzzles
from btc_puzzle_lab.recommend import (
    recommend_engine,
    recommend_pinned_engine,
)
from btc_puzzle_lab.strategy import SAFE_DP

GIB = 1024**3
PUZZLES = {puzzle.id: puzzle for puzzle in load_packaged_full_puzzles()}


def _host(*, gpu: bool = False, cpu_count: int = 8) -> HostCapabilities:
    devices = (
        (
            GpuDevice(
                device_id="GPU-exact",
                name="exact RTX 5090",
                memory_bytes=32 * GIB,
                compute_capability=(12, 0),
                multiprocessor_count=170,
            ),
        )
        if gpu
        else ()
    )
    return HostCapabilities(
        architecture="x86_64",
        cpu_count=cpu_count,
        memory_bytes=128 * GIB,
        disk_free_bytes=100 * GIB,
        gpus=devices,
    )


@pytest.mark.parametrize(
    ("puzzle_id", "gpu", "expected"),
    [
        (16, False, ("sequential", "cpu")),
        (71, False, ("keyhunt", "cpu")),
        (71, True, ("bitcrack", "gpu")),
        (140, False, ("kangaroo", "cpu")),
        # Automatic policy does not opt into manually provisioned RCKangaroo.
        (140, True, ("kangaroo", "cpu")),
    ],
)
def test_shared_planner_selection_matrix(puzzle_id, gpu, expected):
    choice = recommend_engine(PUZZLES[puzzle_id], _host(gpu=gpu))

    assert (choice.engine, choice.resource) == expected
    assert choice.ok
    assert "shared planner" in choice.reason


def test_cpu_only_restricts_an_otherwise_gpu_selection():
    choice = recommend_engine(PUZZLES[71], _host(gpu=True), cpu_only=True)

    assert (choice.engine, choice.resource) == ("keyhunt", "cpu")
    assert choice.ok
    assert "restricted to CPU" in choice.reason


@pytest.mark.parametrize(
    ("puzzle_id", "engine", "gpu"),
    [
        # An explicit pin may override the built-in-range preference.
        (16, "keyhunt", False),
        # It may explicitly choose address brute force despite a public key.
        (140, "bitcrack", True),
    ],
)
def test_pin_can_override_soft_planner_preferences(puzzle_id, engine, gpu):
    choice = recommend_pinned_engine(
        PUZZLES[puzzle_id],
        engine,
        capabilities=_host(gpu=gpu),
        pin_source="--engine",
    )

    assert choice.engine == engine
    assert choice.ok
    assert choice.reason == "pinned by --engine"


@pytest.mark.parametrize(
    ("puzzle_id", "engine", "gpu", "blocker"),
    [
        (71, "kangaroo", False, "PUBLIC_KEY_REQUIRED"),
        (71, "bitcrack", False, "GPU_MISSING"),
    ],
)
def test_pin_cannot_override_hard_planner_blockers(puzzle_id, engine, gpu, blocker):
    choice = recommend_pinned_engine(
        PUZZLES[puzzle_id],
        engine,
        capabilities=_host(gpu=gpu),
        pin_source="BTC_PUZZLE_LAB_ENGINE",
    )

    assert choice.engine == engine
    assert not choice.ok
    assert blocker in choice.blocked
    assert choice.reason == "pinned by BTC_PUZZLE_LAB_ENGINE"


def test_pinned_rck_enables_manual_provisioning_and_keeps_device_identity():
    choice = recommend_pinned_engine(
        PUZZLES[140],
        "rckangaroo",
        capabilities=_host(gpu=True),
        pin_source="--engine",
    )

    assert (choice.engine, choice.resource) == ("rckangaroo", "gpu")
    assert choice.provisioning is ProvisioningPolicy.MANUAL_REQUIRED
    assert choice.device_id == "GPU-exact"
    assert choice.dp == SAFE_DP
    assert choice.ok


@pytest.mark.parametrize("engine", ["window", "not-an-engine"])
def test_non_planner_engines_cannot_use_the_pinned_api(engine):
    with pytest.raises(ValueError, match="planner EngineName"):
        recommend_pinned_engine(
            PUZZLES[71],
            engine,
            capabilities=_host(),
            pin_source="--engine",
        )
