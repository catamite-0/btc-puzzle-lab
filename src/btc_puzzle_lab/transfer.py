"""Sweep transfer for puzzle hits: build, sign, dry-run or broadcast."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass

import base58
import requests

from btc_puzzle_lab.crypto import (
    compressed_pubkey,
    decode_segwit_address,
    is_valid_btc_address,
    match_privkey_address,
    normalize_privkey_hex,
    privkey_bytes,
    sign_sighash_der,
    uncompressed_pubkey,
)
from btc_puzzle_lab.hits import Hit
from btc_puzzle_lab.paths import STATE_DIR
from btc_puzzle_lab.settings import (
    TransferSettings,
    get_transfer_settings,
    validate_transfer_settings,
)


@dataclass(frozen=True)
class TransferResult:
    status: str
    message: str
    send_amount: int | None = None
    fee: int | None = None
    fee_rate: int | None = None
    txid: str | None = None
    dry_run_path: str | None = None
    tx_fingerprint: str | None = None


def serialize_varint(val: int) -> bytes:
    if val < 253:
        return bytes([val])
    if val <= 0xFFFF:
        return b"\xfd" + val.to_bytes(2, "little")
    if val <= 0xFFFFFFFF:
        return b"\xfe" + val.to_bytes(4, "little")
    return b"\xff" + val.to_bytes(8, "little")


def address_to_script_pubkey(addr: str) -> bytes:
    if addr.startswith("1"):
        decoded = base58.b58decode_check(addr)
        if len(decoded) != 21 or decoded[0] != 0:
            raise ValueError("invalid legacy P2PKH address")
        return b"\x76\xa9\x14" + decoded[1:] + b"\x88\xac"
    if addr.startswith("3"):
        decoded = base58.b58decode_check(addr)
        if len(decoded) != 21 or decoded[0] != 5:
            raise ValueError("invalid P2SH address")
        return b"\xa9\x14" + decoded[1:] + b"\x87"
    if addr.lower().startswith("bc1"):
        wit_ver, wit_prog = decode_segwit_address("bc", addr)
        return bytes([wit_ver, len(wit_prog)]) + wit_prog
    raise ValueError(f"unsupported BTC address format: {addr}")


def legacy_sighash(
    tx_version: int,
    inputs: list[dict],
    outputs: list[dict],
    locktime: int,
    index: int,
    script_pubkey: bytes,
) -> bytes:
    res = tx_version.to_bytes(4, "little")
    res += serialize_varint(len(inputs))
    for idx, inp in enumerate(inputs):
        res += bytes.fromhex(inp["txid"])[::-1]
        res += inp["vout"].to_bytes(4, "little")
        if idx == index:
            res += serialize_varint(len(script_pubkey)) + script_pubkey
        else:
            res += b"\x00"
        res += inp["sequence"].to_bytes(4, "little")
    res += serialize_varint(len(outputs))
    for out in outputs:
        res += out["value"].to_bytes(8, "little")
        res += serialize_varint(len(out["scriptPubKey"])) + out["scriptPubKey"]
    res += locktime.to_bytes(4, "little")
    res += int(1).to_bytes(4, "little")  # SIGHASH_ALL
    return hashlib.sha256(hashlib.sha256(res).digest()).digest()


def bip143_sighash(
    tx_version: int,
    inputs: list[dict],
    outputs: list[dict],
    locktime: int,
    index: int,
    script_pubkey: bytes,
    amount: int,
) -> bytes:
    def double_sha256(b: bytes) -> bytes:
        return hashlib.sha256(hashlib.sha256(b).digest()).digest()

    prevouts = b"".join(
        bytes.fromhex(inp["txid"])[::-1] + inp["vout"].to_bytes(4, "little") for inp in inputs
    )
    sequences = b"".join(inp["sequence"].to_bytes(4, "little") for inp in inputs)
    outs = b"".join(
        out["value"].to_bytes(8, "little")
        + serialize_varint(len(out["scriptPubKey"]))
        + out["scriptPubKey"]
        for out in outputs
    )
    hash160 = script_pubkey[2:]
    script_code = b"\x76\xa9\x14" + hash160 + b"\x88\xac"
    target = inputs[index]
    preimage = tx_version.to_bytes(4, "little")
    preimage += double_sha256(prevouts)
    preimage += double_sha256(sequences)
    preimage += bytes.fromhex(target["txid"])[::-1]
    preimage += target["vout"].to_bytes(4, "little")
    preimage += serialize_varint(len(script_code)) + script_code
    preimage += amount.to_bytes(8, "little")
    preimage += target["sequence"].to_bytes(4, "little")
    preimage += double_sha256(outs)
    preimage += locktime.to_bytes(4, "little")
    preimage += int(1).to_bytes(4, "little")
    return double_sha256(preimage)


def estimate_tx_vbytes(
    num_inputs: int,
    to_script_pubkey_len: int,
    addr_type: str,
    *,
    compressed: bool = True,
) -> int:
    if addr_type == "segwit":
        return 11 + num_inputs * 68 + (8 + 1 + to_script_pubkey_len)
    input_size = 148 if compressed else 180
    return 10 + num_inputs * input_size + (8 + 1 + to_script_pubkey_len)


def build_signed_transaction(
    private_key_hex: str,
    utxos: list[dict],
    from_address: str,
    to_address: str,
    fee_rate: int,
    addr_type: str,
    *,
    compressed: bool = True,
) -> tuple[str, int, int]:
    if addr_type == "segwit" and not compressed:
        raise ValueError("P2WPKH requires a compressed pubkey")
    to_script = address_to_script_pubkey(to_address)
    vbytes = estimate_tx_vbytes(len(utxos), len(to_script), addr_type, compressed=compressed)
    fee = vbytes * fee_rate
    total = sum(int(u["value"]) for u in utxos)
    send_amount = total - fee
    if send_amount <= 0:
        return "", send_amount, fee

    inputs = [
        {
            "txid": u["txid"],
            "vout": int(u["vout"]),
            "value": int(u["value"]),
            "sequence": 0xFFFFFFFF,
        }
        for u in utxos
    ]
    outputs = [{"value": send_amount, "scriptPubKey": to_script}]
    pk_bytes = bytes.fromhex(private_key_hex)
    pubkey = compressed_pubkey(pk_bytes) if compressed else uncompressed_pubkey(pk_bytes)
    from_script = address_to_script_pubkey(from_address)
    signatures: list[bytes] = []

    if addr_type == "segwit":
        for idx, inp in enumerate(inputs):
            sighash = bip143_sighash(1, inputs, outputs, 0, idx, from_script, inp["value"])
            signatures.append(sign_sighash_der(pk_bytes, sighash) + b"\x01")
        tx = b"\x01\x00\x00\x00\x00\x01"
        tx += serialize_varint(len(inputs))
        for inp in inputs:
            tx += bytes.fromhex(inp["txid"])[::-1]
            tx += inp["vout"].to_bytes(4, "little")
            tx += b"\x00"
            tx += inp["sequence"].to_bytes(4, "little")
        tx += serialize_varint(len(outputs))
        for out in outputs:
            tx += out["value"].to_bytes(8, "little")
            tx += serialize_varint(len(out["scriptPubKey"])) + out["scriptPubKey"]
        for sig in signatures:
            tx += serialize_varint(2)
            tx += serialize_varint(len(sig)) + sig
            tx += serialize_varint(len(pubkey)) + pubkey
        tx += b"\x00\x00\x00\x00"
    else:
        for idx, _inp in enumerate(inputs):
            sighash = legacy_sighash(1, inputs, outputs, 0, idx, from_script)
            signatures.append(sign_sighash_der(pk_bytes, sighash) + b"\x01")
        tx = b"\x01\x00\x00\x00"
        tx += serialize_varint(len(inputs))
        for idx, inp in enumerate(inputs):
            tx += bytes.fromhex(inp["txid"])[::-1]
            tx += inp["vout"].to_bytes(4, "little")
            sig = signatures[idx]
            script_sig = bytes([len(sig)]) + sig + bytes([len(pubkey)]) + pubkey
            tx += serialize_varint(len(script_sig)) + script_sig
            tx += inp["sequence"].to_bytes(4, "little")
        tx += serialize_varint(len(outputs))
        for out in outputs:
            tx += out["value"].to_bytes(8, "little")
            tx += serialize_varint(len(out["scriptPubKey"])) + out["scriptPubKey"]
        tx += b"\x00\x00\x00\x00"

    return tx.hex(), send_amount, fee


def get_utxos(addr: str, *, timeout: float = 15.0) -> list[dict]:
    url = f"https://blockstream.info/api/address/{addr}/utxo"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def get_fee_rate(settings: TransferSettings, *, timeout: float = 10.0) -> int:
    rate = settings.default_fee_rate
    try:
        resp = requests.get("https://blockstream.info/api/fee-estimates", timeout=timeout)
        resp.raise_for_status()
        estimates = resp.json()
        api_rate = estimates.get("2") or estimates.get("1") or estimates.get("3")
        if api_rate:
            rate = max(1, math.ceil(api_rate))
    except Exception as exc:  # noqa: BLE001
        print(f"fee estimate failed ({exc}); using default {settings.default_fee_rate}")
    if rate > settings.max_fee_rate:
        print(f"fee rate {rate} capped to {settings.max_fee_rate}")
        rate = settings.max_fee_rate
    return rate


def broadcast_tx(tx_hex: str, *, timeout: float = 15.0) -> str:
    errors: list[str] = []
    for url, kind in (
        ("https://blockstream.info/api/tx", "raw"),
        ("https://mempool.space/api/tx", "raw"),
    ):
        try:
            resp = requests.post(url, data=tx_hex, timeout=timeout)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text.strip()
            errors.append(f"{url} HTTP {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    try:
        resp = requests.post(
            "https://api.blockcypher.com/v1/btc/main/txs/push",
            json={"tx": tx_hex},
            timeout=timeout,
        )
        if resp.status_code in (200, 201):
            txid = resp.json().get("tx", {}).get("hash")
            if txid:
                return txid
        errors.append(f"blockcypher HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"blockcypher: {exc}")
    raise RuntimeError("broadcast failed: " + "; ".join(errors))


def _write_dry_run(addr: str, tx_hex: str) -> tuple[str, str]:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(bytes.fromhex(tx_hex)).hexdigest()
    path = STATE_DIR / f"dryrun_{addr}_{fingerprint[:16]}.txhex"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(tx_hex)
        fh.write("\n")
    return str(path), fingerprint


def sweep_hit(
    hit: Hit,
    *,
    settings: TransferSettings | None = None,
    utxos: list[dict] | None = None,
    fee_rate: int | None = None,
    broadcast: bool | None = None,
) -> TransferResult:
    """
    Sweep a recorded hit to AUTO_TRANSFER_DEST_ADDR.
    Defaults: disabled; dry-run when enabled.
    """
    cfg = settings or get_transfer_settings()
    errors = validate_transfer_settings(cfg)
    if not cfg.enabled:
        return TransferResult(status="skipped", message="AUTO_TRANSFER_ENABLED=false")
    if errors:
        return TransferResult(status="error", message="; ".join(errors))
    if not is_valid_btc_address(cfg.dest_addr):
        return TransferResult(status="error", message="invalid destination address")

    try:
        pk_hex = normalize_privkey_hex(hit.private_key_hex)
        pk_bytes = privkey_bytes(pk_hex)
        addr_type, compressed = match_privkey_address(pk_bytes, hit.address)
        resolved_utxos = utxos if utxos is not None else get_utxos(hit.address)
        if not resolved_utxos:
            return TransferResult(status="skipped", message="no UTXOs on source address")
        total = sum(int(u["value"]) for u in resolved_utxos)
        if total < cfg.min_balance_sats:
            return TransferResult(
                status="skipped",
                message=f"balance {total} sats below min {cfg.min_balance_sats}",
            )
        resolved_fee_rate = fee_rate if fee_rate is not None else get_fee_rate(cfg)
        tx_hex, send_amount, fee = build_signed_transaction(
            private_key_hex=pk_hex,
            utxos=resolved_utxos,
            from_address=hit.address,
            to_address=cfg.dest_addr,
            fee_rate=resolved_fee_rate,
            addr_type=addr_type,
            compressed=compressed,
        )
        if send_amount <= 0:
            return TransferResult(
                status="skipped",
                message=f"insufficient for fee (fee={fee})",
                fee=fee,
                fee_rate=resolved_fee_rate,
            )
        if send_amount < cfg.min_send_sats:
            return TransferResult(
                status="skipped",
                message=f"send amount {send_amount} below min {cfg.min_send_sats}",
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
            )

        do_broadcast = (not cfg.dry_run) if broadcast is None else broadcast
        if cfg.dry_run or not do_broadcast:
            path, fingerprint = _write_dry_run(hit.address, tx_hex)
            return TransferResult(
                status="dry_run",
                message="signed tx written locally; not broadcast",
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
                dry_run_path=path,
                tx_fingerprint=fingerprint,
            )

        if not cfg.live_ok:
            return TransferResult(
                status="error",
                message="live broadcast blocked: missing AUTO_TRANSFER_LIVE_CONFIRM",
            )
        txid = broadcast_tx(tx_hex)
        return TransferResult(
            status="broadcast",
            message="broadcast ok",
            send_amount=send_amount,
            fee=fee,
            fee_rate=resolved_fee_rate,
            txid=txid,
            tx_fingerprint=hashlib.sha256(bytes.fromhex(tx_hex)).hexdigest(),
        )
    except Exception as exc:  # noqa: BLE001
        return TransferResult(status="error", message=str(exc))
