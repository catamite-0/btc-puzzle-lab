"""Hit relay for hosts that cannot reach Discord/Telegram.

The VPS posts to a URL it *can* reach (Server酱 / ntfy / a bounce host).
The puzzle solution is never sent in plaintext: it is sealed to an X25519
public key that stays on the operator's open-network machine.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from btc_puzzle_lab.hits import Hit, utc_now
from btc_puzzle_lab.paths import CONFIG_DIR, STATE_DIR
from btc_puzzle_lab.runlog import log_event

TOKEN_PREFIX = "bpl1."
_HKDF_INFO = b"btc-puzzle-lab-relay-v1"
_TOKEN_RE = re.compile(r"bpl1\.[A-Za-z0-9_-]+")
_PUBKEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def relay_secret_path() -> Path:
    return Path(CONFIG_DIR) / "relay-secret"


def relay_outbox_path() -> Path:
    return Path(STATE_DIR) / "relay_outbox.jsonl"


def is_seal_pubkey(value: str) -> bool:
    return bool(_PUBKEY_RE.fullmatch(value.strip()))


def _raw_private(key: X25519PrivateKey) -> bytes:
    return key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def _raw_public(key: X25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def generate_relay_keypair() -> tuple[str, str]:
    """Return (secret_hex, pubkey_hex). Secret stays on the receiving machine."""
    private = X25519PrivateKey.generate()
    public = private.public_key()
    return _raw_private(private).hex(), _raw_public(public).hex()


def write_relay_secret(secret_hex: str, path: Path | None = None) -> Path:
    target = path or relay_secret_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(secret_hex.strip().lower() + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return target


def load_relay_secret(path: Path | None = None) -> str:
    env = os.getenv("RELAY_SEAL_SECRET", "").strip()
    if env:
        return env.lower().removeprefix("0x")
    target = path or relay_secret_path()
    if not target.is_file():
        raise FileNotFoundError(
            f"no relay secret at {target} (run: btc-puzzle-lab relay-keygen)"
        )
    return target.read_text(encoding="utf-8").strip().lower().removeprefix("0x")


def seal_bytes(plaintext: bytes, pubkey_hex: str) -> str:
    pub_hex = pubkey_hex.strip().lower().removeprefix("0x")
    if not is_seal_pubkey(pub_hex):
        raise ValueError("RELAY_SEAL_PUBKEY must be 32-byte X25519 pubkey hex")
    peer = X25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
    ephemeral = X25519PrivateKey.generate()
    shared = ephemeral.exchange(peer)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(shared)
    nonce = secrets.token_bytes(12)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, _HKDF_INFO)
    blob = _raw_public(ephemeral.public_key()) + nonce + ciphertext
    return TOKEN_PREFIX + urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def extract_seal_token(text: str) -> str:
    match = _TOKEN_RE.search(text.strip())
    if not match:
        raise ValueError("no bpl1. token found")
    return match.group(0)


def unseal_bytes(token: str, secret_hex: str) -> bytes:
    raw_token = extract_seal_token(token)
    packed = raw_token[len(TOKEN_PREFIX) :]
    pad = "=" * ((4 - len(packed) % 4) % 4)
    blob = urlsafe_b64decode(packed + pad)
    if len(blob) < 32 + 12 + 16:
        raise ValueError("sealed token is too short")
    eph_pub = blob[:32]
    nonce = blob[32:44]
    ciphertext = blob[44:]
    secret = secret_hex.strip().lower().removeprefix("0x")
    private = X25519PrivateKey.from_private_bytes(bytes.fromhex(secret))
    shared = private.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=_HKDF_INFO,
    ).derive(shared)
    return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, _HKDF_INFO)


def seal_hit(hit: Hit, pubkey_hex: str) -> str:
    payload = {
        "puzzle_id": hit.puzzle_id,
        "address": hit.address,
        "private_key_hex": hit.private_key_hex,
        "engine": hit.engine,
        "found_at": hit.found_at,
    }
    return seal_bytes(json.dumps(payload, separators=(",", ":")).encode("utf-8"), pubkey_hex)


@dataclass(frozen=True)
class RelayResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class UnsealedHit:
    puzzle_id: int
    address: str
    private_key_hex: str
    engine: str = ""
    found_at: str = ""


def unseal_hit(token: str, secret_hex: str | None = None) -> UnsealedHit:
    try:
        raw = unseal_bytes(token, secret_hex or load_relay_secret())
        row = json.loads(raw.decode("utf-8"))
        return UnsealedHit(
            puzzle_id=int(row["puzzle_id"]),
            address=str(row["address"]),
            private_key_hex=str(row["private_key_hex"]),
            engine=str(row.get("engine") or ""),
            found_at=str(row.get("found_at") or ""),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, InvalidTag) as exc:
        raise ValueError(f"could not unseal: {exc}") from exc


def build_relay_message(hit: Hit, *, sealed: str | None = None) -> str:
    """Alert body for a reachable hop. Ciphertext only — never the raw key."""
    lines = [
        "btc-puzzle-lab HIT",
        f"puzzle=#{hit.puzzle_id}",
        f"address={hit.address}",
        f"engine={hit.engine}",
        f"found_at={hit.found_at}",
    ]
    if sealed:
        lines.append(f"sealed={sealed}")
        lines.append("unseal: btc-puzzle-lab unseal --show-key <sealed>")
    else:
        lines.append("solution stayed on the VPS (set RELAY_SEAL_PUBKEY to forward it sealed)")
    if hit.private_key_hex:
        text = "\n".join(lines)
        return text.replace(hit.private_key_hex, "[REDACTED]")
    return "\n".join(lines)


def _append_outbox(row: dict[str, Any]) -> Path:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = relay_outbox_path()
    created = not target.exists()
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    if created:
        os.chmod(target, 0o600)
    return target


def _rewrite_outbox(rows: list[dict[str, Any]]) -> None:
    target = relay_outbox_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    os.chmod(target, 0o600)


def read_outbox() -> list[dict[str, Any]]:
    target = relay_outbox_path()
    if not target.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _relay_headers() -> dict[str, str]:
    token = os.getenv("RELAY_TOKEN", "").strip()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _post_relay(
    url: str,
    text: str,
    *,
    sealed: str | None,
    timeout: float,
    extra: dict[str, Any] | None = None,
) -> RelayResult:
    host = (urlparse(url).hostname or "").lower()
    headers = _relay_headers()
    payload: dict[str, Any] = {
        "title": "btc-puzzle-lab HIT",
        "message": text,
        "body": text,
        "sealed": sealed,
    }
    if extra:
        payload.update(extra)
    try:
        if "ftqq.com" in host or "serverchan" in host:
            resp = requests.post(
                url,
                data={"title": "btc-puzzle-lab HIT", "desp": text},
                headers=headers or None,
                timeout=timeout,
            )
        elif "pushplus.plus" in host:
            resp = requests.post(
                url,
                json={"title": "btc-puzzle-lab HIT", "content": text},
                headers=headers or None,
                timeout=timeout,
            )
        else:
            resp = requests.post(
                url,
                json=payload,
                headers=headers or None,
                timeout=timeout,
            )
        if 200 <= resp.status_code < 300:
            return RelayResult(True, f"http {resp.status_code}")
        return RelayResult(False, f"http {resp.status_code}: {(resp.text or '')[:200]}")
    except requests.RequestException as exc:
        return RelayResult(False, str(exc))


def deliver_relay(
    hit: Hit,
    *,
    url: str,
    seal_pubkey: str = "",
    timeout: float = 15.0,
) -> RelayResult:
    """Seal (if a pubkey is set), persist an outbox row, then POST to the hop."""
    sealed = seal_hit(hit, seal_pubkey) if seal_pubkey.strip() else None
    text = build_relay_message(hit, sealed=sealed)
    row = {
        "ts": utc_now(),
        "puzzle_id": hit.puzzle_id,
        "address": hit.address,
        "url": url,
        "text": text,
        "sealed": sealed,
        "delivered": False,
        "error": "",
    }
    _append_outbox(row)
    result = _post_relay(
        url,
        text,
        sealed=sealed,
        timeout=timeout,
        extra={"puzzle_id": hit.puzzle_id, "address": hit.address},
    )
    rows = read_outbox()
    if rows:
        rows[-1]["delivered"] = result.ok
        rows[-1]["error"] = "" if result.ok else result.message
        _rewrite_outbox(rows)
    log_event(
        "relay_hit",
        puzzle_id=hit.puzzle_id,
        address=hit.address,
        ok=result.ok,
        sealed=bool(sealed),
    )
    return result


def flush_outbox(*, timeout: float = 15.0) -> list[RelayResult]:
    """Retry undelivered relay rows (ciphertext + alert only)."""
    rows = read_outbox()
    results: list[RelayResult] = []
    changed = False
    for row in rows:
        if row.get("delivered"):
            continue
        url = str(row.get("url") or "")
        text = str(row.get("text") or "")
        sealed = row.get("sealed")
        if not url or not text:
            continue
        extra: dict[str, Any] = {}
        if row.get("puzzle_id") is not None:
            extra["puzzle_id"] = row["puzzle_id"]
        if row.get("address"):
            extra["address"] = row["address"]
        result = _post_relay(
            url,
            text,
            sealed=sealed if isinstance(sealed, str) else None,
            timeout=timeout,
            extra=extra or None,
        )
        results.append(result)
        row["delivered"] = result.ok
        row["error"] = "" if result.ok else result.message
        changed = True
    if changed:
        _rewrite_outbox(rows)
    return results
