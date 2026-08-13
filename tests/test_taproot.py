"""Taproot (P2TR) payout destinations: bech32m decoding and output scripts.

Taproot is supported as a *destination* only. Puzzle hits are P2PKH/P2WPKH, and
spending *from* a v1 output needs Schnorr/BIP-341 signing, which this lab does
not implement.
"""

import pytest

from btc_puzzle_lab.crypto import (
    _BECH32_CHARSET,
    BECH32_CONST,
    BECH32M_CONST,
    _bech32_create_checksum,
    _convertbits,
    decode_segwit_address,
    encode_segwit_address,
    is_valid_btc_address,
)
from btc_puzzle_lab.settings import bootstrap_config
from btc_puzzle_lab.transfer import (
    address_to_script_pubkey,
    script_pubkey_to_address,
    witness_opcode,
    witness_version_from_opcode,
)

# BIP-350 test vectors (mainnet, standard program lengths).
P2WPKH = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
P2WSH = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
P2TR = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"


def _reencode(witver: int, witprog: bytes, const: int) -> str:
    """Build an address with a deliberately wrong checksum constant."""
    data = [witver] + (_convertbits(witprog, 8, 5, True) or [])
    combined = data + _bech32_create_checksum("bc", data, const)
    return "bc1" + "".join(_BECH32_CHARSET[d] for d in combined)


def test_taproot_address_is_accepted():
    assert is_valid_btc_address(P2TR)


def test_taproot_output_script_uses_op_1_not_the_raw_version():
    script = address_to_script_pubkey(P2TR)
    # OP_1 (0x51) then a 32-byte push. Emitting the raw version byte 0x01 would
    # have produced a script that does not encode the intended program at all.
    assert script[0] == 0x51
    assert script[1] == 0x20
    assert len(script) == 34
    assert script.hex() == (
        "512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
    )


@pytest.mark.parametrize("address", [P2WPKH, P2WSH, P2TR])
def test_witness_addresses_round_trip_through_their_script(address):
    assert script_pubkey_to_address(address_to_script_pubkey(address)) == address


def test_witness_opcode_mapping():
    assert witness_opcode(0) == 0x00
    assert witness_opcode(1) == 0x51
    assert witness_opcode(16) == 0x60
    assert witness_version_from_opcode(0x00) == 0
    assert witness_version_from_opcode(0x51) == 1
    assert witness_version_from_opcode(0x76) is None  # OP_DUP, i.e. a P2PKH script
    with pytest.raises(ValueError):
        witness_opcode(17)


def test_v0_keeps_bech32_and_v1_requires_bech32m():
    witver0, prog0 = decode_segwit_address("bc", P2WPKH)
    witver1, prog1 = decode_segwit_address("bc", P2TR)
    assert (witver0, len(prog0)) == (0, 20)
    assert (witver1, len(prog1)) == (1, 32)
    # Re-encoding each under the other's constant must be rejected: BIP-350 binds
    # the encoding to the witness version precisely so the two cannot be confused.
    with pytest.raises(ValueError, match="must use bech32"):
        decode_segwit_address("bc", _reencode(0, prog0, BECH32M_CONST))
    with pytest.raises(ValueError, match="must use bech32m"):
        decode_segwit_address("bc", _reencode(1, prog1, BECH32_CONST))


def test_a_corrupted_taproot_address_is_rejected():
    broken = P2TR[:-1] + ("q" if P2TR[-1] != "q" else "p")
    assert not is_valid_btc_address(broken)


def test_undefined_witness_versions_are_refused():
    # Not a typo to tolerate: v2+ has no defined spending rules, so funds sent
    # there are non-standard to relay and anyone-can-spend by consensus.
    addr = _reencode(2, b"\x01" * 32, BECH32M_CONST)
    with pytest.raises(ValueError, match="no defined address type"):
        decode_segwit_address("bc", addr)
    assert not is_valid_btc_address(addr)


def test_non_standard_taproot_program_length_is_refused():
    with pytest.raises(ValueError, match="taproot"):
        encode_segwit_address("bc", 1, b"\x01" * 20)


def test_taproot_can_be_configured_as_the_payout_address():
    update = bootstrap_config(dest_addr=P2TR)
    assert update.dest_addr == P2TR

    from btc_puzzle_lab.settings import get_transfer_settings, validate_transfer_settings

    settings = get_transfer_settings()
    assert settings.dest_addr == P2TR
    assert validate_transfer_settings(settings) == []
