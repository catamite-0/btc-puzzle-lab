"""Minimal secp256k1 helpers for puzzle search and sweep transfers."""

from __future__ import annotations

import hashlib
import re

import base58
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
    encode_dss_signature,
)

# secp256k1 curve order and field prime
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

BTC_ADDR_RE = re.compile(
    r"^(1[a-km-zA-HJ-NP-Z1-9]{25,34}|3[a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[ac-hj-np-z02-9]{11,71})$"
)
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def normalize_privkey_hex(value: str) -> str:
    hx = value.lower().removeprefix("0x").strip()
    if not hx or any(c not in "0123456789abcdef" for c in hx):
        raise ValueError("private key must be hex")
    if len(hx) > 64:
        raise ValueError("private key hex too long")
    return hx.zfill(64)


def privkey_bytes(value: str | int) -> bytes:
    if isinstance(value, int):
        secret = value
    else:
        secret = int(normalize_privkey_hex(value), 16)
    if not (1 <= secret < SECP256K1_N):
        raise ValueError("private key out of secp256k1 range")
    return secret.to_bytes(32, "big")


def _private_key(pk_bytes: bytes) -> ec.EllipticCurvePrivateKey:
    if len(pk_bytes) != 32:
        raise ValueError("private key must be 32 bytes")
    secret = int.from_bytes(pk_bytes, "big")
    if not (1 <= secret < SECP256K1_N):
        raise ValueError("private key out of secp256k1 range")
    return ec.derive_private_key(secret, ec.SECP256K1())


def compressed_pubkey(pk_bytes: bytes) -> bytes:
    numbers = _private_key(pk_bytes).public_key().public_numbers()
    prefix = b"\x02" if numbers.y % 2 == 0 else b"\x03"
    return prefix + numbers.x.to_bytes(32, "big")


def uncompressed_pubkey(pk_bytes: bytes) -> bytes:
    numbers = _private_key(pk_bytes).public_key().public_numbers()
    return b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


def sign_sighash_der(pk_bytes: bytes, sighash: bytes) -> bytes:
    if len(sighash) != 32:
        raise ValueError("sighash must be 32 bytes")
    der = _private_key(pk_bytes).sign(sighash, ec.ECDSA(Prehashed(hashes.SHA256())))
    r, s = decode_dss_signature(der)
    if s > SECP256K1_N // 2:
        s = SECP256K1_N - s
    return encode_dss_signature(r, s)


def verify_sighash(pubkey: bytes, sighash: bytes, der_sig: bytes) -> bool:
    if len(sighash) != 32:
        return False
    try:
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), pubkey)
        pub.verify(der_sig, sighash, ec.ECDSA(Prehashed(hashes.SHA256())))
        return True
    except (InvalidSignature, ValueError):
        return False


def privkey_to_p2pkh_address(pk_bytes: bytes, *, compressed: bool = True) -> str:
    pub = compressed_pubkey(pk_bytes) if compressed else uncompressed_pubkey(pk_bytes)
    payload = b"\x00" + hash160(pub)
    return base58.b58encode_check(payload).decode("ascii")


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= generator[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify(hrp: str, data: list[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(
    data: bytes | list[int], frombits: int, tobits: int, pad: bool = True
) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def decode_segwit_address(hrp: str, addr: str) -> tuple[int, bytes]:
    addr = addr.lower()
    if any(ord(x) < 33 or ord(x) > 126 for x in addr):
        raise ValueError("invalid segwit address characters")
    pos = addr.rfind("1")
    if pos < 1:
        raise ValueError("invalid segwit address separator")
    if addr[:pos] != hrp:
        raise ValueError("segwit address hrp mismatch")
    data_part = addr[pos + 1 :]
    if len(data_part) < 6:
        raise ValueError("segwit address too short")
    try:
        data = [_BECH32_CHARSET.index(c) for c in data_part]
    except ValueError as exc:
        raise ValueError("invalid bech32 character") from exc
    if not _bech32_verify(hrp, data):
        raise ValueError("invalid bech32 checksum")
    decoded = _convertbits(data[1:-6], 5, 8, False)
    if decoded is None:
        raise ValueError("invalid segwit program")
    witver = data[0]
    witprog = bytes(decoded)
    if witver > 16 or not (2 <= len(witprog) <= 40):
        raise ValueError("invalid segwit version/program")
    if witver == 0 and len(witprog) not in (20, 32):
        raise ValueError("invalid v0 segwit program length")
    return witver, witprog


def encode_segwit_address(hrp: str, witver: int, witprog: bytes) -> str:
    if witver > 16 or not (2 <= len(witprog) <= 40):
        raise ValueError("invalid segwit version/program")
    data = [witver] + (_convertbits(witprog, 8, 5, True) or [])
    combined = data + _bech32_create_checksum(hrp, data)
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in combined)


def privkey_to_p2wpkh_address(pk_bytes: bytes) -> str:
    return encode_segwit_address("bc", 0, hash160(compressed_pubkey(pk_bytes)))


def is_valid_btc_address(addr: str) -> bool:
    if not addr or not BTC_ADDR_RE.match(addr):
        return False
    if addr.startswith(("1", "3")):
        try:
            decoded = base58.b58decode_check(addr)
            version = 0 if addr.startswith("1") else 5
            return len(decoded) == 21 and decoded[0] == version
        except Exception:
            return False
    try:
        decode_segwit_address("bc", addr)
        return True
    except Exception:
        return False


def address_hash160(address: str) -> bytes:
    raw = base58.b58decode_check(address)
    if len(raw) != 21 or raw[0] != 0:
        raise ValueError("only mainnet P2PKH addresses are supported")
    return raw[1:]


def match_privkey_address(pk_bytes: bytes, address: str) -> tuple[str, bool]:
    """
    Return (addr_type, compressed) if the private key derives `address`.
    addr_type is 'legacy' or 'segwit'.
    """
    if address == privkey_to_p2pkh_address(pk_bytes, compressed=True):
        return "legacy", True
    if address == privkey_to_p2pkh_address(pk_bytes, compressed=False):
        return "legacy", False
    if address == privkey_to_p2wpkh_address(pk_bytes):
        return "segwit", True
    raise ValueError("source address does not match private key derivatives")


def _mod_inverse(a: int, m: int) -> int:
    return pow(a, -1, m)


def _point_add(
    p1: tuple[int, int] | None, p2: tuple[int, int] | None
) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % SECP256K1_P == 0:
        return None
    if x1 == x2 and y1 == y2:
        s = (3 * x1 * x1 * _mod_inverse(2 * y1, SECP256K1_P)) % SECP256K1_P
    else:
        s = ((y2 - y1) * _mod_inverse(x2 - x1, SECP256K1_P)) % SECP256K1_P
    x3 = (s * s - x1 - x2) % SECP256K1_P
    y3 = (s * (x1 - x3) - y1) % SECP256K1_P
    return x3, y3


def _scalar_mul(k: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _point_to_compressed(point: tuple[int, int]) -> bytes:
    x, y = point
    prefix = b"\x02" if y % 2 == 0 else b"\x03"
    return prefix + x.to_bytes(32, "big")


def sequential_find_p2pkh(
    target_address: str,
    start: int,
    end: int,
    *,
    progress_every: int = 50_000,
) -> int | None:
    """Scan inclusive [start, end]; return matching private key int or None."""
    if start < 1 or end >= SECP256K1_N or start > end:
        raise ValueError("invalid search range")
    target = address_hash160(target_address)
    g = (SECP256K1_GX, SECP256K1_GY)
    point = _scalar_mul(start, g)
    if point is None:
        raise ValueError("invalid start point")
    checked = 0
    for secret in range(start, end + 1):
        pub = _point_to_compressed(point)
        if hash160(pub) == target:
            return secret
        point = _point_add(point, g)
        if point is None:
            raise RuntimeError("unexpected point at infinity during scan")
        checked += 1
        if progress_every and checked % progress_every == 0:
            print(f"… scanned {checked:,} keys (at {secret:x})", flush=True)
    return None
