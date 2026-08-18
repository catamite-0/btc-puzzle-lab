from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import requests

from btc_puzzle_lab.crypto import match_privkey_address, normalize_privkey_hex, privkey_bytes
from btc_puzzle_lab.hits import Hit, read_hits
from btc_puzzle_lab.runlog import log_event


@dataclass(frozen=True)
class AuditResult:
    hit: Hit
    address_ok: bool
    derived_address: str
    balance_sats: int | None
    error: str | None = None
    addr_type: str | None = None


def verify_hit(hit: Hit) -> AuditResult:
    try:
        normalize_privkey_hex(hit.private_key_hex)
        pk = privkey_bytes(hit.private_key_hex)
        addr_type, _compressed = match_privkey_address(pk, hit.address)
        return AuditResult(
            hit=hit,
            address_ok=True,
            derived_address=hit.address,
            balance_sats=None,
            addr_type=addr_type,
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
                    addr_type=result.addr_type,
                )
            except Exception as exc:  # noqa: BLE001
                result = AuditResult(
                    hit=result.hit,
                    address_ok=result.address_ok,
                    derived_address=result.derived_address,
                    balance_sats=None,
                    error=f"balance lookup failed: {exc}",
                    addr_type=result.addr_type,
                )
        results.append(result)
    failures = sum(1 for r in results if not r.address_ok or r.error)
    log_event(
        "audit_complete",
        hits=len(results),
        failures=failures,
        check_balance=check_balance,
    )
    return results


def export_audit_report(results: list[AuditResult], path: Path) -> Path:
    payload = {
        "schema_version": 1,
        "results": [
            {
                "puzzle_id": r.hit.puzzle_id,
                "address": r.hit.address,
                "engine": r.hit.engine,
                "found_at": r.hit.found_at,
                "address_ok": r.address_ok,
                "derived_address": r.derived_address,
                "balance_sats": r.balance_sats,
                "addr_type": r.addr_type,
                "error": r.error,
                # deliberately omit private_key_hex
            }
            for r in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log_event("audit_export", path=str(path), hits=len(results))
    return path

