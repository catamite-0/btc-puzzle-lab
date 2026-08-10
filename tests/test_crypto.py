from btc_puzzle_lab.catalog import get_puzzle, load_puzzles
from btc_puzzle_lab.crypto import (
    privkey_bytes,
    privkey_to_p2pkh_address,
    sequential_find_p2pkh,
    split_range,
)


def test_catalog_solutions_derive_addresses():
    checked = 0
    for puzzle in load_puzzles():
        # Unsolved entries (full import-catalog) legitimately carry no solution.
        if puzzle.practice_solution is None:
            continue
        derived = privkey_to_p2pkh_address(privkey_bytes(puzzle.practice_solution))
        assert derived == puzzle.address
        checked += 1
    assert checked, "catalog exposed no solved entries to verify"


def test_split_range_covers_exactly():
    chunks = split_range(10, 20, 3)
    assert chunks == [(10, 13), (14, 17), (18, 20)]
    flat = []
    for lo, hi in chunks:
        flat.extend(range(lo, hi + 1))
    assert flat == list(range(10, 21))


def test_progress_callback_emits_rate():
    puzzle = get_puzzle(5)
    center = puzzle.practice_solution
    events: list[tuple[int, int, float]] = []

    def on_progress(checked: int, secret: int, rate: float) -> None:
        events.append((checked, secret, rate))

    found = sequential_find_p2pkh(
        puzzle.address,
        center - 8,
        center + 8,
        progress_every=4,
        on_progress=on_progress,
    )
    assert found == center
    assert events
    assert events[0][0] == 4
    assert events[0][2] >= 0
