from __future__ import annotations

from dataclasses import dataclass

import requests

from btc_puzzle_lab.crypto import normalize_privkey_hex, privkey_bytes, privkey_to_p2pkh_address
from btc_puzzle_lab.hits import Hit, read_hits


@dataclass(frozen=True)
class AuditResult:
    hit: Hit
    address_ok: bool
    derived_address: str
    balance_sats: int | None
    error: str | None = None


def verify_hit(hit: Hit) -> AuditResult:
    try:
        # Normalize/validate hex even though we only compare the derived address.
        normalize_privkey_hex(hit.private_key_hex)
        pk = privkey_bytes(hit.private_key_hex)
        derived = privkey_to_p2pkh_address(pk)
        return AuditResult(
            hit=hit,
            address_ok=derived == hit.address,
            derived_address=derived,
            balance_sats=None,
        )
    except Exception as exc:  # noqa: BLE001 - surface to CLI
        return AuditResult(
            hit=hit,
            address_ok=False,
            derived_address="",
            balance_sats=None,
            error=str(exc),
        )


def fetch_balance_sats(address: str, *, timeout: float = 15.0) -> int:
    url = f"https://mempool.space/api/address/{address}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    chain = data.get("chain_stats", {})
    mempool = data.get("mempool_stats", {})
    funded = int(chain.get("funded_txo_sum", 0)) + int(mempool.get("funded_txo_sum", 0))
    spent = int(chain.get("spent_txo_sum", 0)) + int(mempool.get("spent_txo_sum", 0))
    return funded - spent


def audit_hits(*, check_balance: bool = False) -> list[AuditResult]:
    results: list[AuditResult] = []
    for hit in read_hits():
        result = verify_hit(hit)
        if check_balance and result.address_ok and not result.error:
            try:
                balance = fetch_balance_sats(hit.address)
                result = AuditResult(
                    hit=result.hit,
                    address_ok=result.address_ok,
                    derived_address=result.derived_address,
                    balance_sats=balance,
                )
            except Exception as exc:  # noqa: BLE001
                result = AuditResult(
                    hit=result.hit,
                    address_ok=result.address_ok,
                    derived_address=result.derived_address,
                    balance_sats=None,
                    error=f"balance lookup failed: {exc}",
                )
        results.append(result)
    return results
