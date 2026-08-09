"""Hit notifications — never include private keys or signed tx hex.

Channels (any combination):
- generic HTTPS webhook (Discord/Slack/ntfy-compatible JSON or plain text)
- Telegram bot API
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from btc_puzzle_lab.audit import AuditResult
from btc_puzzle_lab.hits import Hit
from btc_puzzle_lab.runlog import log_event
from btc_puzzle_lab.settings import NotifySettings, get_notify_settings
from btc_puzzle_lab.transfer import TransferResult


@dataclass(frozen=True)
class NotifyResult:
    channel: str
    ok: bool
    message: str


def build_hit_message(
    hit: Hit,
    *,
    audit: AuditResult | None = None,
    transfer: TransferResult | None = None,
) -> str:
    """Human-readable alert body with no private-key material."""
    lines = [
        "btc-puzzle-lab HIT",
        f"puzzle=#{hit.puzzle_id}",
        f"address={hit.address}",
        f"engine={hit.engine}",
        f"found_at={hit.found_at}",
        f"verified={hit.verified}",
    ]
    if audit is not None:
        mark = "ok" if audit.address_ok and not audit.error else "fail"
        lines.append(f"audit={mark}")
        if audit.balance_sats is not None:
            lines.append(f"balance_sats={audit.balance_sats}")
        if audit.error:
            lines.append(f"audit_error={audit.error}")
    if transfer is not None:
        lines.append(f"transfer={transfer.status}: {transfer.message}")
    lines.append("private key kept in local state/HITS.jsonl only")
    return "\n".join(lines)


def _webhook_payload(text: str, webhook_url: str) -> dict[str, Any] | str:
    host = (urlparse(webhook_url).hostname or "").lower()
    # Discord incoming webhooks expect {"content": "..."}.
    if "discord.com" in host or "discordapp.com" in host:
        return {"content": text[:1900]}
    # Slack incoming webhooks expect {"text": "..."}.
    if "hooks.slack.com" in host:
        return {"text": text}
    # ntfy and generic endpoints accept JSON body or plain text; prefer JSON.
    return {"message": text, "title": "btc-puzzle-lab HIT", "body": text}


def send_webhook(text: str, *, url: str, timeout: float = 15.0) -> NotifyResult:
    payload = _webhook_payload(text, url)
    try:
        if isinstance(payload, str):
            resp = requests.post(
                url,
                data=payload.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
                timeout=timeout,
            )
        else:
            resp = requests.post(url, json=payload, timeout=timeout)
        if 200 <= resp.status_code < 300:
            return NotifyResult("webhook", True, f"http {resp.status_code}")
        return NotifyResult(
            "webhook",
            False,
            f"http {resp.status_code}: {(resp.text or '')[:200]}",
        )
    except requests.RequestException as exc:
        return NotifyResult("webhook", False, str(exc))


def send_telegram(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    timeout: float = 15.0,
) -> NotifyResult:
    api = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(
            api,
            json={"chat_id": chat_id, "text": text},
            timeout=timeout,
        )
        if 200 <= resp.status_code < 300:
            data = resp.json() if resp.content else {}
            if data.get("ok") is False:
                return NotifyResult("telegram", False, json.dumps(data)[:200])
            return NotifyResult("telegram", True, "ok")
        return NotifyResult(
            "telegram",
            False,
            f"http {resp.status_code}: {(resp.text or '')[:200]}",
        )
    except requests.RequestException as exc:
        return NotifyResult("telegram", False, str(exc))


def notify_hit(
    hit: Hit,
    *,
    audit: AuditResult | None = None,
    transfer: TransferResult | None = None,
    settings: NotifySettings | None = None,
) -> list[NotifyResult]:
    cfg = settings or get_notify_settings()
    if not cfg.enabled:
        return [NotifyResult("none", True, "NOTIFY_ENABLED=false")]
    if not cfg.configured:
        return [NotifyResult("none", False, "notify enabled but no webhook/telegram configured")]

    text = build_hit_message(hit, audit=audit, transfer=transfer)
    # Hard safety: never ship key material even if a caller regresses.
    if hit.private_key_hex and hit.private_key_hex in text:
        text = text.replace(hit.private_key_hex, "[REDACTED]")

    results: list[NotifyResult] = []
    if cfg.webhook_url:
        results.append(send_webhook(text, url=cfg.webhook_url))
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        results.append(
            send_telegram(
                text,
                bot_token=cfg.telegram_bot_token,
                chat_id=cfg.telegram_chat_id,
            )
        )
    log_event(
        "notify_hit",
        puzzle_id=hit.puzzle_id,
        address=hit.address,
        channels=[r.channel for r in results],
        ok=all(r.ok for r in results) if results else False,
    )
    return results


def format_notify_results(results: list[NotifyResult]) -> str:
    if not results:
        return "notify: (none)"
    lines = []
    for item in results:
        mark = "ok" if item.ok else "fail"
        lines.append(f"notify[{mark}] {item.channel}: {item.message}")
    return "\n".join(lines)
