"""Sweep transfer for puzzle hits: build, sign, dry-run or broadcast."""

from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import dataclass
from pathlib import Path

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
from btc_puzzle_lab.runlog import log_event
from btc_puzzle_lab.settings import (
    TransferSettings,
    get_transfer_settings,
    validate_transfer_settings,
)

SEQUENCE_FINAL = 0xFFFFFFFF
SEQUENCE_RBF = 0xFFFFFFFD

# Prefer these block targets per fee strategy when estimates are available.
_FEE_STRATEGY_BLOCKS = {
    "economy": ("6", "3", "2", "1"),
    "normal": ("2", "3", "1", "6"),
    "priority": ("1", "2", "3"),
}


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
    input_count: int | None = None
    rbf: bool | None = None


@dataclass(frozen=True)
class DryRunVerifyResult:
    ok: bool
    path: str
    message: str
    fingerprint: str | None = None
    version: int | None = None
    input_count: int | None = None
    output_count: int | None = None
    size_bytes: int | None = None


def serialize_varint(val: int) -> bytes:
    if val < 253:
        return bytes([val])
    if val <= 0xFFFF:
        return b"\xfd" + val.to_bytes(2, "little")
    if val <= 0xFFFFFFFF:
        return b"\xfe" + val.to_bytes(4, "little")
    return b"\xff" + val.to_bytes(8, "little")


def read_varint(buf: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(buf):
        raise ValueError("truncated varint")
    first = buf[offset]
    if first < 253:
        return first, offset + 1
    if first == 253:
        return int.from_bytes(buf[offset + 1 : offset + 3], "little"), offset + 3
    if first == 254:
        return int.from_bytes(buf[offset + 1 : offset + 5], "little"), offset + 5
    return int.from_bytes(buf[offset + 1 : offset + 9], "little"), offset + 9


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


def select_utxos_for_sweep(utxos: list[dict]) -> list[dict]:
    """Consolidate all confirmed-looking UTXOs, largest first for stable ordering."""
    cleaned: list[dict] = []
    for u in utxos:
        value = int(u["value"])
        if value <= 0:
            continue
        cleaned.append(
            {
                "txid": u["txid"],
                "vout": int(u["vout"]),
                "value": value,
            }
        )
    cleaned.sort(key=lambda u: (-u["value"], u["txid"], u["vout"]))
    return cleaned


def build_signed_transaction(
    private_key_hex: str,
    utxos: list[dict],
    from_address: str,
    to_address: str,
    fee_rate: int,
    addr_type: str,
    *,
    compressed: bool = True,
    rbf: bool = True,
) -> tuple[str, int, int]:
    if addr_type == "segwit" and not compressed:
        raise ValueError("P2WPKH requires a compressed pubkey")
    selected = select_utxos_for_sweep(utxos)
    if not selected:
        return "", 0, 0
    to_script = address_to_script_pubkey(to_address)
    vbytes = estimate_tx_vbytes(len(selected), len(to_script), addr_type, compressed=compressed)
    fee = vbytes * fee_rate
    total = sum(int(u["value"]) for u in selected)
    send_amount = total - fee
    if send_amount <= 0:
        return "", send_amount, fee

    sequence = SEQUENCE_RBF if rbf else SEQUENCE_FINAL
    inputs = [
        {
            "txid": u["txid"],
            "vout": int(u["vout"]),
            "value": int(u["value"]),
            "sequence": sequence,
        }
        for u in selected
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


def _pick_fee_from_estimates(estimates: dict, settings: TransferSettings) -> int | None:
    keys = list(_FEE_STRATEGY_BLOCKS.get(settings.fee_strategy, _FEE_STRATEGY_BLOCKS["normal"]))
    target = str(settings.fee_target_blocks)
    if target not in keys:
        keys.insert(0, target)
    for key in keys:
        api_rate = estimates.get(key)
        if api_rate is not None:
            return max(1, math.ceil(float(api_rate)))
    # Fall back to any numeric estimate.
    for value in estimates.values():
        try:
            return max(1, math.ceil(float(value)))
        except (TypeError, ValueError):
            continue
    return None


def get_fee_rate(settings: TransferSettings, *, timeout: float = 10.0) -> int:
    rate = settings.default_fee_rate
    try:
        resp = requests.get("https://blockstream.info/api/fee-estimates", timeout=timeout)
        resp.raise_for_status()
        estimates = resp.json()
        picked = _pick_fee_from_estimates(estimates, settings)
        if picked is not None:
            rate = picked
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


def parse_tx_skeleton(tx_hex: str) -> tuple[int, int, int, int]:
    """Return (version, input_count, output_count, size_bytes) without validating scripts."""
    raw = bytes.fromhex(tx_hex.strip())
    if len(raw) < 10:
        raise ValueError("tx too short")
    version = struct.unpack_from("<I", raw, 0)[0]
    offset = 4
    # Detect segwit marker/flag
    if offset + 2 <= len(raw) and raw[offset] == 0x00 and raw[offset + 1] == 0x01:
        offset += 2
        segwit = True
    else:
        segwit = False
    vin, offset = read_varint(raw, offset)
    for _ in range(vin):
        offset += 32 + 4  # txid + vout
        script_len, offset = read_varint(raw, offset)
        offset += script_len + 4  # script + sequence
    vout, offset = read_varint(raw, offset)
    for _ in range(vout):
        offset += 8
        script_len, offset = read_varint(raw, offset)
        offset += script_len
    if segwit:
        # Skip witnesses loosely: for each input, read item count then items.
        for _ in range(vin):
            n_items, offset = read_varint(raw, offset)
            for _i in range(n_items):
                item_len, offset = read_varint(raw, offset)
                offset += item_len
    if offset + 4 > len(raw):
        raise ValueError("truncated transaction (locktime)")
    return version, vin, vout, len(raw)


def verify_dry_run_file(path: Path | str) -> DryRunVerifyResult:
    """Validate a dry-run artifact structurally; never echoes tx hex."""
    target = Path(path)
    if not target.is_file():
        return DryRunVerifyResult(ok=False, path=str(target), message="file not found")
    try:
        text = target.read_text(encoding="utf-8").strip()
        if not text or any(c not in "0123456789abcdefABCDEF" for c in text):
            return DryRunVerifyResult(
                ok=False, path=str(target), message="file is not hex payload"
            )
        fingerprint = hashlib.sha256(bytes.fromhex(text)).hexdigest()
        version, vin, vout, size = parse_tx_skeleton(text)
        name = target.name
        if "dryrun_" in name and fingerprint[:16] not in name:
            return DryRunVerifyResult(
                ok=False,
                path=str(target),
                message="fingerprint does not match filename",
                fingerprint=fingerprint,
                version=version,
                input_count=vin,
                output_count=vout,
                size_bytes=size,
            )
        if vin < 1 or vout < 1:
            return DryRunVerifyResult(
                ok=False,
                path=str(target),
                message="tx must have at least one input and one output",
                fingerprint=fingerprint,
                version=version,
                input_count=vin,
                output_count=vout,
                size_bytes=size,
            )
        log_event(
            "dryrun_verify",
            path=str(target),
            ok=True,
            inputs=vin,
            outputs=vout,
            size_bytes=size,
            fingerprint=fingerprint[:16],
        )
        return DryRunVerifyResult(
            ok=True,
            path=str(target),
            message="dry-run tx structural check ok",
            fingerprint=fingerprint,
            version=version,
            input_count=vin,
            output_count=vout,
            size_bytes=size,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("dryrun_verify", path=str(target), ok=False, error=str(exc))
        return DryRunVerifyResult(ok=False, path=str(target), message=str(exc))


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
        resolved_utxos = select_utxos_for_sweep(
            utxos if utxos is not None else get_utxos(hit.address)
        )
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
            rbf=cfg.rbf,
        )
        if send_amount <= 0:
            return TransferResult(
                status="skipped",
                message=f"insufficient for fee (fee={fee})",
                fee=fee,
                fee_rate=resolved_fee_rate,
                input_count=len(resolved_utxos),
                rbf=cfg.rbf,
            )
        if send_amount < cfg.min_send_sats:
            return TransferResult(
                status="skipped",
                message=f"send amount {send_amount} below min {cfg.min_send_sats}",
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
                input_count=len(resolved_utxos),
                rbf=cfg.rbf,
            )

        do_broadcast = (not cfg.dry_run) if broadcast is None else broadcast
        if cfg.dry_run or not do_broadcast:
            path, fingerprint = _write_dry_run(hit.address, tx_hex)
            log_event(
                "transfer_dry_run",
                puzzle_id=hit.puzzle_id,
                address=hit.address,
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
                inputs=len(resolved_utxos),
                rbf=cfg.rbf,
                fingerprint=fingerprint[:16],
            )
            return TransferResult(
                status="dry_run",
                message="signed tx written locally; not broadcast",
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
                dry_run_path=path,
                tx_fingerprint=fingerprint,
                input_count=len(resolved_utxos),
                rbf=cfg.rbf,
            )

        if not cfg.live_ok:
            return TransferResult(
                status="error",
                message="live broadcast blocked: missing AUTO_TRANSFER_LIVE_CONFIRM",
            )
        txid = broadcast_tx(tx_hex)
        fingerprint = hashlib.sha256(bytes.fromhex(tx_hex)).hexdigest()
        log_event(
            "transfer_broadcast",
            puzzle_id=hit.puzzle_id,
            address=hit.address,
            send_amount=send_amount,
            fee=fee,
            fee_rate=resolved_fee_rate,
            inputs=len(resolved_utxos),
            rbf=cfg.rbf,
            txid=txid,
            fingerprint=fingerprint[:16],
        )
        return TransferResult(
            status="broadcast",
            message="broadcast ok",
            send_amount=send_amount,
            fee=fee,
            fee_rate=resolved_fee_rate,
            txid=txid,
            tx_fingerprint=fingerprint,
            input_count=len(resolved_utxos),
            rbf=cfg.rbf,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("transfer_error", puzzle_id=hit.puzzle_id, error=str(exc))
        return TransferResult(status="error", message=str(exc))
