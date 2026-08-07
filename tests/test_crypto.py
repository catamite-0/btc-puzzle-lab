from btc_puzzle_lab.catalog import get_puzzle
from btc_puzzle_lab.crypto import privkey_bytes, privkey_to_p2pkh_address


def test_catalog_solutions_derive_addresses():
    for puzzle_id in (20, 40):
        puzzle = get_puzzle(puzzle_id)
        assert puzzle.practice_solution is not None
        derived = privkey_to_p2pkh_address(privkey_bytes(puzzle.practice_solution))
        assert derived == puzzle.address
