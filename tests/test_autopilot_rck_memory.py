import pytest

from btc_puzzle_lab.autopilot.rck_memory import (
    rck_base_allocation_bytes,
)


def test_5090_source_derived_allocation_floors_are_exact() -> None:
    assert rck_base_allocation_bytes(sm_count=170, compute_capability=(12, 0)) == (
        456_523_776,
        3_150_510_104,
    )


@pytest.mark.parametrize(
    ("capability", "below", "at"),
    [((8, 9), 31, 32), ((12, 0), 23, 24)],
)
def test_inverse_sm_threshold_increases_the_device_floor(
    capability: tuple[int, int], below: int, at: int
) -> None:
    assert (
        rck_base_allocation_bytes(sm_count=at, compute_capability=capability)[1]
        > rck_base_allocation_bytes(sm_count=below, compute_capability=capability)[1]
    )


@pytest.mark.parametrize(
    ("sm_count", "capability"),
    [
        (True, (12, 0)),
        (1.0, (12, 0)),
        (0, (12, 0)),
        (257, (12, 0)),
        (1, (12, 0)),
        (170, [12, 0]),
        (170, (True, 0)),
        (170, (9, 0)),
    ],
)
def test_invalid_or_unsupported_topology_is_rejected(sm_count: object, capability: object) -> None:
    with pytest.raises(ValueError):
        rck_base_allocation_bytes(  # type: ignore[arg-type]
            sm_count=sm_count, compute_capability=capability
        )
