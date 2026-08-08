from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import btc_puzzle_lab.transfer as transfer_mod
from btc_puzzle_lab.crypto import (
    compressed_pubkey,
    privkey_to_p2pkh_address,
    privkey_to_p2wpkh_address,
    sign_sighash_der,
    uncompressed_pubkey,
    verify_sighash,
)
from btc_puzzle_lab.hits import Hit
from btc_puzzle_lab.settings import TransferSettings
from btc_puzzle_lab.transfer import (
    SEQUENCE_FINAL,
    _pick_fee_from_estimates,
    address_to_script_pubkey,
    bip143_sighash,
    build_signed_transaction,
    estimate_tx_vbytes,
    legacy_sighash,
    select_utxos_for_sweep,
    sweep_hit,
    verify_dry_run_file,
)


def _settings(**overrides) -> TransferSettings:
    base = dict(
        enabled=True,
        dry_run=True,
        dest_addr="1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH",
        live_confirm="",
        min_balance_sats=1000,
        min_send_sats=546,
        default_fee_rate=10,
        max_fee_rate=250,
        fee_strategy="normal",
        fee_target_blocks=2,
        rbf=True,
    )
    base.update(overrides)
    return TransferSettings(**base)


def test_address_scripts_and_vbytes():
    legacy = "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
    segwit = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    assert address_to_script_pubkey(legacy) == bytes.fromhex(
        "76a914751e76e8199196d454941c45d1b3a323f1433bd688ac"
    )
    assert address_to_script_pubkey(segwit) == bytes.fromhex(
        "0014751e76e8199196d454941c45d1b3a323f1433bd6"
    )
    assert estimate_tx_vbytes(1, 25, "legacy") == 192
    assert estimate_tx_vbytes(1, 25, "legacy", compressed=False) == 224
    assert estimate_tx_vbytes(1, 22, "segwit") == 110
    assert estimate_tx_vbytes(2, 25, "legacy") == 340


def test_legacy_signing_roundtrip():
    private_key_hex = "00" * 31 + "02"
    pk = bytes.fromhex(private_key_hex)
    from_address = privkey_to_p2pkh_address(pk)
    to_address = "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
    utxos = [{
        "txid": "e1e9f1a2384f9de1a86c67341e8c71d604b7b39a3ea3ef5d22ea6c3fcaef0b33",
        "vout": 1,
        "value": 1_000_000,
    }]
    tx_hex, send_amount, fee = build_signed_transaction(
        private_key_hex=private_key_hex,
        utxos=utxos,
        from_address=from_address,
        to_address=to_address,
        fee_rate=10,
        addr_type="legacy",
        rbf=False,
    )
    assert tx_hex
    assert send_amount + fee == 1_000_000

    inputs = [{
        "txid": utxos[0]["txid"],
        "vout": 1,
        "value": 1_000_000,
        "sequence": SEQUENCE_FINAL,
    }]
    outputs = [{
        "value": send_amount,
        "scriptPubKey": address_to_script_pubkey(to_address),
    }]
    sighash = legacy_sighash(
        1, inputs, outputs, 0, 0, address_to_script_pubkey(from_address)
    )
    assert verify_sighash(compressed_pubkey(pk), sighash, sign_sighash_der(pk, sighash))


def test_rbf_and_multi_utxo_sweep():
    private_key_hex = "00" * 31 + "02"
    pk = bytes.fromhex(private_key_hex)
    from_address = privkey_to_p2pkh_address(pk)
    utxos = [
        {
            "txid": "aa" * 32,
            "vout": 0,
            "value": 400_000,
        },
        {
            "txid": "bb" * 32,
            "vout": 1,
            "value": 600_000,
        },
    ]
    selected = select_utxos_for_sweep(utxos)
    assert [u["value"] for u in selected] == [600_000, 400_000]
    tx_hex, send_amount, fee = build_signed_transaction(
        private_key_hex=private_key_hex,
        utxos=utxos,
        from_address=from_address,
        to_address="1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH",
        fee_rate=10,
        addr_type="legacy",
        rbf=True,
    )
    assert tx_hex
    assert send_amount + fee == 1_000_000
    # sequence 0xfffffffd little-endian appears twice (two inputs)
    assert tx_hex.count("fdffffff") == 2


def test_uncompressed_and_segwit_signing():
    private_key_hex = "00" * 31 + "02"
    pk = bytes.fromhex(private_key_hex)
    utxos = [{
        "txid": "e1e9f1a2384f9de1a86c67341e8c71d604b7b39a3ea3ef5d22ea6c3fcaef0b33",
        "vout": 1,
        "value": 1_000_000,
    }]

    from_u = privkey_to_p2pkh_address(pk, compressed=False)
    tx_u, send_u, fee_u = build_signed_transaction(
        private_key_hex=private_key_hex,
        utxos=utxos,
        from_address=from_u,
        to_address="1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH",
        fee_rate=10,
        addr_type="legacy",
        compressed=False,
        rbf=False,
    )
    assert fee_u == 2240
    assert uncompressed_pubkey(pk).hex() in tx_u
    assert send_u + fee_u == 1_000_000

    from_s = privkey_to_p2wpkh_address(pk)
    to_s = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    tx_s, send_s, fee_s = build_signed_transaction(
        private_key_hex=private_key_hex,
        utxos=utxos,
        from_address=from_s,
        to_address=to_s,
        fee_rate=10,
        addr_type="segwit",
        rbf=False,
    )
    assert tx_s and send_s + fee_s == 1_000_000
    inputs = [{
        "txid": utxos[0]["txid"],
        "vout": 1,
        "value": 1_000_000,
        "sequence": SEQUENCE_FINAL,
    }]
    outputs = [{"value": send_s, "scriptPubKey": address_to_script_pubkey(to_s)}]
    sighash = bip143_sighash(
        1, inputs, outputs, 0, 0, address_to_script_pubkey(from_s), 1_000_000
    )
    assert verify_sighash(compressed_pubkey(pk), sighash, sign_sighash_der(pk, sighash))


def test_fee_strategy_prefers_target_blocks():
    estimates = {"1": 40.2, "2": 20.1, "6": 5.5}
    economy = _pick_fee_from_estimates(
        estimates, _settings(fee_strategy="economy", fee_target_blocks=6)
    )
    priority = _pick_fee_from_estimates(
        estimates, _settings(fee_strategy="priority", fee_target_blocks=1)
    )
    assert economy == 6
    assert priority == 41


def test_sweep_dry_run_verify_and_gates(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(transfer_mod, "STATE_DIR", tmp_path)
    pk_hex = "00" * 31 + "02"
    pk = bytes.fromhex(pk_hex)
    addr = privkey_to_p2pkh_address(pk)
    hit = Hit(
        puzzle_id=20,
        address=addr,
        private_key_hex=pk_hex,
        engine="test",
        found_at="2026-01-01T00:00:00Z",
        verified=True,
    )
    utxos = [{
        "txid": "e1e9f1a2384f9de1a86c67341e8c71d604b7b39a3ea3ef5d22ea6c3fcaef0b33",
        "vout": 1,
        "value": 1_000_000,
    }]

    skipped = sweep_hit(hit, settings=_settings(enabled=False), utxos=utxos, fee_rate=10)
    assert skipped.status == "skipped"

    mismatch = sweep_hit(
        Hit(**{**hit.__dict__, "address": "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"}),
        settings=_settings(),
        utxos=utxos,
        fee_rate=10,
    )
    assert mismatch.status == "error"

    dry = sweep_hit(hit, settings=_settings(), utxos=utxos, fee_rate=10)
    assert dry.status == "dry_run"
    assert dry.dry_run_path
    assert dry.rbf is True
    assert dry.input_count == 1
    path = Path(dry.dry_run_path)
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert dry.tx_fingerprint

    verified = verify_dry_run_file(path)
    assert verified.ok is True
    assert verified.input_count == 1
    assert verified.output_count == 1
    # Ensure verify path does not require printing hex; content remains on disk only.
    assert path.read_text(encoding="utf-8").strip()

    with patch.object(transfer_mod, "broadcast_tx", return_value="txid-demo") as broadcast:
        live = sweep_hit(
            hit,
            settings=_settings(
                dry_run=False,
                live_confirm="I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC",
            ),
            utxos=utxos,
            fee_rate=10,
        )
        assert live.status == "broadcast"
        assert live.txid == "txid-demo"
        broadcast.assert_called_once()
