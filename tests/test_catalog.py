from btc_puzzle_lab.catalog import load_puzzles


def test_catalog_contains_practice_targets():
    ids = {p.id for p in load_puzzles()}
    assert {1, 5, 10, 16, 20, 24, 28, 32, 40, 45, 50}.issubset(ids)


def test_puzzle_ranges_match_bit_width():
    for puzzle in load_puzzles():
        assert puzzle.range_start == 1 << (puzzle.bits - 1)
        assert puzzle.range_end == (1 << puzzle.bits) - 1
        assert puzzle.range_start <= puzzle.practice_solution <= puzzle.range_end


def test_engine_defaults_match_host_class():
    for puzzle in load_puzzles():
        if puzzle.bits <= 20:
            assert puzzle.engine_default == "sequential"
        else:
            assert puzzle.engine_default == "window"
