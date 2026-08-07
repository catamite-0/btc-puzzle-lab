from btc_puzzle_lab.catalog import load_puzzles


def test_catalog_contains_practice_targets():
    ids = {p.id for p in load_puzzles()}
    assert {20, 40}.issubset(ids)


def test_puzzle_ranges_match_bit_width():
    for puzzle in load_puzzles():
        assert puzzle.range_start == 1 << (puzzle.bits - 1)
        assert puzzle.range_end == (1 << puzzle.bits) - 1
        assert puzzle.range_start <= puzzle.practice_solution <= puzzle.range_end
