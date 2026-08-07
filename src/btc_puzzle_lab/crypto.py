"""Minimal secp256k1 helpers for P2PKH practice searches."""

from __future__ import annotations

import hashlib

import base58
from cryptography.hazmat.primitives.asymmetric import ec

# secp256k1 curve order and field prime
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
SECP256K1_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
SECP256K1_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


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


def compressed_pubkey(pk_bytes: bytes) -> bytes:
    numbers = ec.derive_private_key(
        int.from_bytes(pk_bytes, "big"), ec.SECP256K1()
    ).public_key().public_numbers()
    prefix = b"\x02" if numbers.y % 2 == 0 else b"\x03"
    return prefix + numbers.x.to_bytes(32, "big")


def privkey_to_p2pkh_address(pk_bytes: bytes) -> str:
    payload = b"\x00" + hash160(compressed_pubkey(pk_bytes))
    return base58.b58encode_check(payload).decode("ascii")


def address_hash160(address: str) -> bytes:
    raw = base58.b58decode_check(address)
    if len(raw) != 21 or raw[0] != 0:
        raise ValueError("only mainnet P2PKH addresses are supported")
    return raw[1:]


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
        # point double
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
    """
    Scan inclusive [start, end] with incremental point addition.
    Returns the matching private key integer, or None.
    """
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
