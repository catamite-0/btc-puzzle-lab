"""Hit notifications — never include private keys or signed tx hex.

Channels (any combination):
- generic HTTPS webhook (Discord/Slack/ntfy-compatible JSON or plain text)
- Telegram bot API
"""

from __future__ import annotations

import json
import re
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


_COLOURS: dict[str, int] = {
    "broadcast": 0x2ECC71,  # green — funds actually moved
    "dry_run": 0x3498DB,  # blue — signed locally, nothing sent
    "skipped": 0xF1C40F,  # amber — a safety gate held
    "error": 0xE74C3C,  # red — audit or sweep failed
}
_MEMPOOL = "https://mempool.space"


def _btc(sats: int) -> str:
    return f"{sats / 100_000_000:.8f} BTC ({sats:,} sats)"


def _field(name: str, value: str, *, inline: bool = True) -> dict[str, Any]:
    return {"name": name, "value": value, "inline": inline}


def build_hit_embed(
    hit: Hit,
    *,
    audit: AuditResult | None = None,
    transfer: TransferResult | None = None,
) -> dict[str, Any]:
    """Discord rich embed for a hit. Carries no key material or signed tx hex."""
    colour = _COLOURS["skipped"]
    if audit is not None and (not audit.address_ok or audit.error):
        colour = _COLOURS["error"]
    elif transfer is not None:
        colour = _COLOURS.get(transfer.status, _COLOURS["skipped"])

    fields = [
        _field(
            "Address",
            f"[`{hit.address}`]({_MEMPOOL}/address/{hit.address})",
            inline=False,
        ),
        _field("Engine", f"`{hit.engine}`"),
        _field("Key verified", "✅ yes" if hit.verified else "⚠️ unverified"),
    ]

    if audit is not None:
        if audit.error:
            fields.append(_field("Audit", f"❌ {audit.error}"))
        elif audit.address_ok:
            kind = f" · {audit.addr_type}" if audit.addr_type else ""
            fields.append(_field("Audit", f"✅ key derives address{kind}"))
        else:
            fields.append(_field("Audit", "❌ address mismatch"))
        if audit.balance_sats is not None:
            fields.append(_field("Balance", _btc(audit.balance_sats)))

    if transfer is not None:
        lines = [f"**{transfer.status}** — {transfer.message}"]
        if transfer.dest_addr:
            lines.append(f"→ [`{transfer.dest_addr}`]({_MEMPOOL}/address/{transfer.dest_addr})")
        if transfer.send_amount is not None:
            lines.append(f"amount: {_btc(transfer.send_amount)}")
        if transfer.fee is not None:
            rate = f" @ {transfer.fee_rate} sat/vB" if transfer.fee_rate else ""
            lines.append(f"fee: {transfer.fee:,} sats{rate}")
        if transfer.txid:
            lines.append(f"txid: [`{transfer.txid}`]({_MEMPOOL}/tx/{transfer.txid})")
        fields.append(_field("Sweep", "\n".join(lines), inline=False))

    return {
        "title": f"🎯 Puzzle #{hit.puzzle_id} solved",
        "color": colour,
        "fields": fields,
        "footer": {"text": "btc-puzzle-lab · private key never leaves state/HITS.jsonl"},
        "timestamp": hit.found_at,
    }


def _webhook_payload(
    text: str,
    webhook_url: str,
    embed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    host = (urlparse(webhook_url).hostname or "").lower()
    # Discord incoming webhooks take {"content": ...} or a rich {"embeds": [...]}.
    if "discord.com" in host or "discordapp.com" in host:
        return {"embeds": [embed]} if embed else {"content": text[:1900]}
    # Slack incoming webhooks expect {"text": "..."}.
    if "hooks.slack.com" in host:
        return {"text": text}
    # ntfy and generic endpoints accept JSON body or plain text; prefer JSON.
    return {"message": text, "title": "btc-puzzle-lab HIT", "body": text}


def send_webhook(
    text: str,
    *,
    url: str,
    embed: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> NotifyResult:
    payload = _webhook_payload(text, url, embed)
    try:
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
    skip_relay: bool = False,
) -> list[NotifyResult]:
    cfg = settings or get_notify_settings()
    send_chat = cfg.enabled and bool(
        cfg.webhook_url or (cfg.telegram_bot_token and cfg.telegram_chat_id)
    )
    send_relay = bool(cfg.relay_url) and not skip_relay
    if not send_chat and not send_relay:
        if not cfg.enabled:
            return [NotifyResult("none", True, "NOTIFY_ENABLED=false")]
        if skip_relay:
            return [NotifyResult("none", True, "hub ingest: chat unset, relay skipped")]
        return [
            NotifyResult(
                "none",
                False,
                "notify enabled but no webhook/telegram/relay configured",
            )
        ]

    text = build_hit_message(hit, audit=audit, transfer=transfer)
    embed = build_hit_embed(hit, audit=audit, transfer=transfer)
    # Hard safety: never ship key material even if a caller regresses. The embed is
    # scrubbed through its serialised form so a leak in any nested field is caught.
    if hit.private_key_hex:
        text = text.replace(hit.private_key_hex, "[REDACTED]")
        raw = json.dumps(embed)
        if hit.private_key_hex.lower() in raw.lower():
            embed = json.loads(
                re.sub(re.escape(hit.private_key_hex), "[REDACTED]", raw, flags=re.IGNORECASE)
            )

    results: list[NotifyResult] = []
    if send_chat and cfg.webhook_url:
        results.append(send_webhook(text, url=cfg.webhook_url, embed=embed))
    if send_chat and cfg.telegram_bot_token and cfg.telegram_chat_id:
        results.append(
            send_telegram(
                text,
                bot_token=cfg.telegram_bot_token,
                chat_id=cfg.telegram_chat_id,
            )
        )
    if send_relay:
        from btc_puzzle_lab.relay import deliver_relay

        relay = deliver_relay(
            hit,
            url=cfg.relay_url,
            seal_pubkey=cfg.relay_seal_pubkey,
        )
        results.append(NotifyResult("relay", relay.ok, relay.message))
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
