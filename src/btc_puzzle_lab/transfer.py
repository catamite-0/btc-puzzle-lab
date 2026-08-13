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
    encode_segwit_address,
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
    dest_addr: str | None = None
    vsize: int | None = None
    chain_status: str | None = None


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
    dest_addr: str | None = None
    send_amount: int | None = None
    vsize: int | None = None


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


def witness_opcode(witver: int) -> int:
    """Script opcode for a witness version.

    v0 is OP_0 (0x00); v1..v16 are OP_1..OP_16 (0x51..0x60), *not* the raw version
    number. Emitting 0x01 for a Taproot output would build a script that does not
    encode the intended program at all.
    """
    if not (0 <= witver <= 16):
        raise ValueError(f"invalid witness version: {witver}")
    return 0x00 if witver == 0 else 0x50 + witver


def witness_version_from_opcode(opcode: int) -> int | None:
    """Inverse of :func:`witness_opcode`, or None when this is not a witness push."""
    if opcode == 0x00:
        return 0
    if 0x51 <= opcode <= 0x60:
        return opcode - 0x50
    return None


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
        return bytes([witness_opcode(wit_ver), len(wit_prog)]) + wit_prog
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


def select_utxos_for_sweep(
    utxos: list[dict],
    *,
    confirmed_only: bool = True,
) -> list[dict]:
    """Consolidate spendable UTXOs, largest first for stable ordering.

    When ``confirmed_only`` is true (default), skip UTXOs whose API status
    explicitly marks ``confirmed=false``. Injected fixtures without a status
    field are treated as spendable.
    """
    cleaned: list[dict] = []
    for u in utxos:
        value = int(u["value"])
        if value <= 0:
            continue
        if confirmed_only:
            status = u.get("status")
            if isinstance(status, dict) and status.get("confirmed") is False:
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


def txid_from_hex(tx_hex: str) -> str:
    raw = bytes.fromhex(tx_hex.strip())
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()


def _non_witness_size(raw: bytes) -> int:
    """Byte length of the transaction excluding witness data (BIP141 base)."""
    if len(raw) < 10:
        raise ValueError("tx too short")
    offset = 4
    if offset + 2 <= len(raw) and raw[offset] == 0x00 and raw[offset + 1] == 0x01:
        offset += 2
        segwit = True
    else:
        segwit = False
    vin, offset = read_varint(raw, offset)
    for _ in range(vin):
        offset += 32 + 4
        script_len, offset = read_varint(raw, offset)
        offset += script_len + 4
    vout, offset = read_varint(raw, offset)
    for _ in range(vout):
        offset += 8
        script_len, offset = read_varint(raw, offset)
        offset += script_len
    if segwit:
        # Skip witnesses; base size is everything before witness + locktime.
        witness_start = offset
        for _ in range(vin):
            n_items, offset = read_varint(raw, offset)
            for _i in range(n_items):
                item_len, offset = read_varint(raw, offset)
                offset += item_len
        # non-witness = prefix through outputs + locktime (4), excluding marker/flag/witness
        # prefix = version(4) + vin..outputs, without the 2-byte marker/flag
        return (witness_start - 2) + 4
    return len(raw)


def tx_vsize(tx_hex: str) -> int:
    """BIP141 virtual size: ceil(weight / 4)."""
    raw = bytes.fromhex(tx_hex.strip())
    if len(raw) >= 6 and raw[4] == 0x00 and raw[5] == 0x01:
        base = _non_witness_size(raw)
        weight = base * 3 + len(raw)
        return (weight + 3) // 4
    return len(raw)


def _assemble_signed_tx(
    *,
    private_key_hex: str,
    selected: list[dict],
    from_address: str,
    to_address: str,
    send_amount: int,
    addr_type: str,
    compressed: bool,
    rbf: bool,
) -> str:
    sequence = SEQUENCE_RBF if rbf else SEQUENCE_FINAL
    to_script = address_to_script_pubkey(to_address)
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
    return tx.hex()


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
    confirmed_only: bool = True,
) -> tuple[str, int, int]:
    if addr_type == "segwit" and not compressed:
        raise ValueError("P2WPKH requires a compressed pubkey")
    selected = select_utxos_for_sweep(utxos, confirmed_only=confirmed_only)
    if not selected:
        return "", 0, 0
    to_script = address_to_script_pubkey(to_address)
    vbytes = estimate_tx_vbytes(len(selected), len(to_script), addr_type, compressed=compressed)
    fee = vbytes * fee_rate
    total = sum(int(u["value"]) for u in selected)
    tx_hex = ""
    send_amount = 0
    # Iterate: DER length can change vsize when the send amount / sighash changes.
    for _ in range(5):
        send_amount = total - fee
        if send_amount <= 0:
            return "", send_amount, fee
        tx_hex = _assemble_signed_tx(
            private_key_hex=private_key_hex,
            selected=selected,
            from_address=from_address,
            to_address=to_address,
            send_amount=send_amount,
            addr_type=addr_type,
            compressed=compressed,
            rbf=rbf,
        )
        actual_fee = tx_vsize(tx_hex) * fee_rate
        if actual_fee == fee:
            return tx_hex, send_amount, fee
        fee = actual_fee
    # Last resort: keep conservation of value even if rate drifts by 1 sat/vB.
    send_amount = total - fee
    if send_amount <= 0 or not tx_hex:
        return "", send_amount, fee
    tx_hex = _assemble_signed_tx(
        private_key_hex=private_key_hex,
        selected=selected,
        from_address=from_address,
        to_address=to_address,
        send_amount=send_amount,
        addr_type=addr_type,
        compressed=compressed,
        rbf=rbf,
    )
    return tx_hex, send_amount, fee


def get_utxos(addr: str, *, timeout: float = 15.0) -> list[dict]:
    errors: list[str] = []
    for base in (
        "https://blockstream.info/api",
        "https://mempool.space/api",
    ):
        url = f"{base}/address/{addr}/utxo"
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            errors.append(f"{url}: unexpected payload")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    raise RuntimeError("utxo fetch failed: " + "; ".join(errors))


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
    last_err: Exception | None = None
    for url in (
        "https://blockstream.info/api/fee-estimates",
        "https://mempool.space/api/v1/fees/recommended",
    ):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            payload = resp.json()
            if "fastestFee" in payload:
                # mempool.space recommended fees (sat/vB)
                key = {
                    "economy": "hourFee",
                    "normal": "halfHourFee",
                    "priority": "fastestFee",
                }.get(settings.fee_strategy, "halfHourFee")
                rate = max(1, int(payload.get(key) or payload["fastestFee"]))
            else:
                picked = _pick_fee_from_estimates(payload, settings)
                if picked is not None:
                    rate = picked
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    else:
        print(f"fee estimate failed ({last_err}); using default {settings.default_fee_rate}")
    if rate > settings.max_fee_rate:
        print(f"fee rate {rate} capped to {settings.max_fee_rate}")
        rate = settings.max_fee_rate
    return rate


def get_tx_status(txid: str, *, timeout: float = 10.0) -> str:
    """Return confirmed | mempool | missing | unknown for a txid."""
    for base in (
        "https://blockstream.info/api",
        "https://mempool.space/api",
    ):
        try:
            resp = requests.get(f"{base}/tx/{txid}", timeout=timeout)
            if resp.status_code == 404:
                return "missing"
            if resp.status_code != 200:
                continue
            data = resp.json()
            status = data.get("status") or {}
            if status.get("confirmed"):
                return "confirmed"
            return "mempool"
        except Exception:  # noqa: BLE001
            continue
    return "unknown"


def broadcast_tx(tx_hex: str, *, timeout: float = 15.0) -> str:
    txid = txid_from_hex(tx_hex)
    errors: list[str] = []
    for url in (
        "https://blockstream.info/api/tx",
        "https://mempool.space/api/tx",
    ):
        try:
            resp = requests.post(url, data=tx_hex, timeout=timeout)
            body = (resp.text or "").strip()
            if resp.status_code == 200 and body:
                return body
            lower = body.lower()
            if resp.status_code in {400, 409} and (
                "already" in lower or "txn-already" in lower or txid in lower
            ):
                return txid
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
            pushed = resp.json().get("tx", {}).get("hash")
            if pushed:
                return pushed
        body = (resp.text or "").lower()
        if resp.status_code in {400, 409} and ("already" in body or txid in body):
            return txid
        errors.append(f"blockcypher HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"blockcypher: {exc}")
    # Last-chance: maybe a previous attempt landed it.
    status = get_tx_status(txid, timeout=timeout)
    if status in {"mempool", "confirmed"}:
        return txid
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


def parse_tx_outputs(tx_hex: str) -> list[tuple[int, bytes]]:
    """Return [(value_sats, script_pubkey), ...] for each output."""
    raw = bytes.fromhex(tx_hex.strip())
    offset = 4
    if offset + 2 <= len(raw) and raw[offset] == 0x00 and raw[offset + 1] == 0x01:
        offset += 2
    vin, offset = read_varint(raw, offset)
    for _ in range(vin):
        offset += 32 + 4
        script_len, offset = read_varint(raw, offset)
        offset += script_len + 4
    vout, offset = read_varint(raw, offset)
    outs: list[tuple[int, bytes]] = []
    for _ in range(vout):
        value = int.from_bytes(raw[offset : offset + 8], "little")
        offset += 8
        script_len, offset = read_varint(raw, offset)
        script = raw[offset : offset + script_len]
        offset += script_len
        outs.append((value, script))
    return outs


def script_pubkey_to_address(script: bytes) -> str | None:
    """Best-effort address decode for common single-key scripts."""
    if len(script) == 25 and script.startswith(b"\x76\xa9\x14") and script.endswith(b"\x88\xac"):
        payload = b"\x00" + script[3:23]
        return base58.b58encode_check(payload).decode()
    if len(script) == 23 and script.startswith(b"\xa9\x14") and script.endswith(b"\x87"):
        payload = b"\x05" + script[2:22]
        return base58.b58encode_check(payload).decode()
    # Witness programs: <version opcode> <push len> <program>. Covers P2WPKH,
    # P2WSH and P2TR without a per-type branch.
    if 4 <= len(script) <= 42 and script[1] == len(script) - 2:
        witver = witness_version_from_opcode(script[0])
        if witver is not None:
            try:
                return encode_segwit_address("bc", witver, script[2:])
            except ValueError:
                return None
    return None


def verify_dry_run_file(
    path: Path | str,
    *,
    expected_dest: str | None = None,
    min_send_sats: int | None = None,
) -> DryRunVerifyResult:
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
        vsize = tx_vsize(text)
        outs = parse_tx_outputs(text)
        dest_addr = None
        send_amount = None
        if outs:
            send_amount = outs[0][0]
            dest_addr = script_pubkey_to_address(outs[0][1])
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
                dest_addr=dest_addr,
                send_amount=send_amount,
                vsize=vsize,
            )
        if vin < 1 or vout != 1:
            return DryRunVerifyResult(
                ok=False,
                path=str(target),
                message="sweep tx must have >=1 input and exactly 1 output",
                fingerprint=fingerprint,
                version=version,
                input_count=vin,
                output_count=vout,
                size_bytes=size,
                dest_addr=dest_addr,
                send_amount=send_amount,
                vsize=vsize,
            )
        if expected_dest:
            try:
                expected_script = address_to_script_pubkey(expected_dest)
            except ValueError as exc:
                return DryRunVerifyResult(
                    ok=False,
                    path=str(target),
                    message=f"invalid expected dest: {exc}",
                    fingerprint=fingerprint,
                    dest_addr=dest_addr,
                    send_amount=send_amount,
                    vsize=vsize,
                )
            if outs[0][1] != expected_script:
                return DryRunVerifyResult(
                    ok=False,
                    path=str(target),
                    message="output script does not match AUTO_TRANSFER_DEST_ADDR",
                    fingerprint=fingerprint,
                    version=version,
                    input_count=vin,
                    output_count=vout,
                    size_bytes=size,
                    dest_addr=dest_addr,
                    send_amount=send_amount,
                    vsize=vsize,
                )
        if min_send_sats is not None and send_amount is not None and send_amount < min_send_sats:
            return DryRunVerifyResult(
                ok=False,
                path=str(target),
                message=f"send amount {send_amount} below min {min_send_sats}",
                fingerprint=fingerprint,
                version=version,
                input_count=vin,
                output_count=vout,
                size_bytes=size,
                dest_addr=dest_addr,
                send_amount=send_amount,
                vsize=vsize,
            )
        log_event(
            "dryrun_verify",
            path=str(target),
            ok=True,
            inputs=vin,
            outputs=vout,
            size_bytes=size,
            vsize=vsize,
            fingerprint=fingerprint[:16],
            dest=dest_addr,
            send_amount=send_amount,
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
            dest_addr=dest_addr,
            send_amount=send_amount,
            vsize=vsize,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("dryrun_verify", path=str(target), ok=False, error=str(exc))
        return DryRunVerifyResult(ok=False, path=str(target), message=str(exc))


def format_transfer_policy(settings: TransferSettings) -> str:
    mode = "dry-run" if settings.dry_run else ("live" if settings.live_ok else "live-blocked")
    return (
        f"enabled={settings.enabled} mode={mode} dest={settings.dest_addr or '(empty)'} "
        f"confirmed_only={settings.confirmed_only} rbf={settings.rbf} "
        f"fee_strategy={settings.fee_strategy} max_fee_rate={settings.max_fee_rate} "
        f"max_fee_sats={settings.max_fee_sats}"
    )


def sweep_hit(
    hit: Hit,
    *,
    settings: TransferSettings | None = None,
    utxos: list[dict] | None = None,
    fee_rate: int | None = None,
    broadcast: bool | None = None,
    confirmed_only: bool | None = None,
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
    use_confirmed_only = cfg.confirmed_only if confirmed_only is None else confirmed_only

    try:
        pk_hex = normalize_privkey_hex(hit.private_key_hex)
        pk_bytes = privkey_bytes(pk_hex)
        addr_type, compressed = match_privkey_address(pk_bytes, hit.address)
        resolved_utxos = select_utxos_for_sweep(
            utxos if utxos is not None else get_utxos(hit.address),
            confirmed_only=use_confirmed_only,
        )
        if not resolved_utxos:
            msg = (
                "no confirmed UTXOs on source address"
                if use_confirmed_only
                else "no UTXOs on source address"
            )
            return TransferResult(status="skipped", message=msg, dest_addr=cfg.dest_addr)
        total = sum(int(u["value"]) for u in resolved_utxos)
        if total < cfg.min_balance_sats:
            return TransferResult(
                status="skipped",
                message=f"balance {total} sats below min {cfg.min_balance_sats}",
                dest_addr=cfg.dest_addr,
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
            confirmed_only=use_confirmed_only,
        )
        vsize = tx_vsize(tx_hex) if tx_hex else None
        if send_amount <= 0:
            return TransferResult(
                status="skipped",
                message=f"insufficient for fee (fee={fee})",
                fee=fee,
                fee_rate=resolved_fee_rate,
                input_count=len(resolved_utxos),
                rbf=cfg.rbf,
                dest_addr=cfg.dest_addr,
                vsize=vsize,
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
                dest_addr=cfg.dest_addr,
                vsize=vsize,
            )
        if fee > cfg.max_fee_sats:
            return TransferResult(
                status="skipped",
                message=f"fee {fee} exceeds AUTO_TRANSFER_MAX_FEE_SATS={cfg.max_fee_sats}",
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
                input_count=len(resolved_utxos),
                rbf=cfg.rbf,
                dest_addr=cfg.dest_addr,
                vsize=vsize,
            )
        if vsize and resolved_fee_rate and (fee / vsize) > cfg.max_fee_rate + 1e-9:
            return TransferResult(
                status="skipped",
                message=(
                    f"effective fee rate {fee / vsize:.2f} exceeds "
                    f"AUTO_TRANSFER_MAX_FEE_RATE={cfg.max_fee_rate}"
                ),
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
                input_count=len(resolved_utxos),
                rbf=cfg.rbf,
                dest_addr=cfg.dest_addr,
                vsize=vsize,
            )

        do_broadcast = (not cfg.dry_run) if broadcast is None else broadcast
        if cfg.dry_run or not do_broadcast:
            path, fingerprint = _write_dry_run(hit.address, tx_hex)
            log_event(
                "transfer_dry_run",
                puzzle_id=hit.puzzle_id,
                address=hit.address,
                dest=cfg.dest_addr,
                send_amount=send_amount,
                fee=fee,
                fee_rate=resolved_fee_rate,
                vsize=vsize,
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
                dest_addr=cfg.dest_addr,
                vsize=vsize,
            )

        if not cfg.live_ok:
            return TransferResult(
                status="error",
                message="live broadcast blocked: missing AUTO_TRANSFER_LIVE_CONFIRM",
                dest_addr=cfg.dest_addr,
            )
        txid = broadcast_tx(tx_hex)
        chain_status = get_tx_status(txid)
        fingerprint = hashlib.sha256(bytes.fromhex(tx_hex)).hexdigest()
        log_event(
            "transfer_broadcast",
            puzzle_id=hit.puzzle_id,
            address=hit.address,
            dest=cfg.dest_addr,
            send_amount=send_amount,
            fee=fee,
            fee_rate=resolved_fee_rate,
            vsize=vsize,
            inputs=len(resolved_utxos),
            rbf=cfg.rbf,
            txid=txid,
            chain_status=chain_status,
            fingerprint=fingerprint[:16],
        )
        return TransferResult(
            status="broadcast",
            message=f"broadcast ok ({chain_status})",
            send_amount=send_amount,
            fee=fee,
            fee_rate=resolved_fee_rate,
            txid=txid,
            tx_fingerprint=fingerprint,
            input_count=len(resolved_utxos),
            rbf=cfg.rbf,
            dest_addr=cfg.dest_addr,
            vsize=vsize,
            chain_status=chain_status,
        )
    except Exception as exc:  # noqa: BLE001
        log_event("transfer_error", puzzle_id=hit.puzzle_id, error=str(exc))
        return TransferResult(status="error", message=str(exc), dest_addr=cfg.dest_addr)


def broadcast_dry_run_file(
    path: Path | str,
    *,
    settings: TransferSettings | None = None,
) -> TransferResult:
    """Broadcast a previously written dry-run artifact (requires live confirm)."""
    cfg = settings or get_transfer_settings()
    if not cfg.enabled:
        return TransferResult(status="skipped", message="AUTO_TRANSFER_ENABLED=false")
    if not cfg.live_ok:
        return TransferResult(
            status="error",
            message="broadcast-dry-run requires AUTO_TRANSFER_LIVE_CONFIRM",
            dest_addr=cfg.dest_addr,
        )
    if cfg.dry_run:
        return TransferResult(
            status="error",
            message="set AUTO_TRANSFER_DRY_RUN=false before broadcast-dry-run",
            dest_addr=cfg.dest_addr,
        )
    verify = verify_dry_run_file(
        path,
        expected_dest=cfg.dest_addr or None,
        min_send_sats=cfg.min_send_sats,
    )
    if not verify.ok:
        return TransferResult(
            status="error",
            message=f"dry-run verify failed: {verify.message}",
            dest_addr=cfg.dest_addr,
            dry_run_path=str(path),
        )
    tx_hex = Path(path).read_text(encoding="utf-8").strip()
    try:
        txid = broadcast_tx(tx_hex)
        chain_status = get_tx_status(txid)
        log_event(
            "transfer_broadcast",
            source="dry_run_file",
            path=str(path),
            dest=cfg.dest_addr,
            send_amount=verify.send_amount,
            txid=txid,
            chain_status=chain_status,
            fingerprint=(verify.fingerprint or "")[:16],
        )
        return TransferResult(
            status="broadcast",
            message=f"broadcast ok ({chain_status})",
            send_amount=verify.send_amount,
            txid=txid,
            tx_fingerprint=verify.fingerprint,
            input_count=verify.input_count,
            dest_addr=verify.dest_addr or cfg.dest_addr,
            vsize=verify.vsize,
            chain_status=chain_status,
            dry_run_path=str(path),
        )
    except Exception as exc:  # noqa: BLE001
        log_event("transfer_error", source="dry_run_file", path=str(path), error=str(exc))
        return TransferResult(
            status="error",
            message=str(exc),
            dest_addr=cfg.dest_addr,
            dry_run_path=str(path),
        )
