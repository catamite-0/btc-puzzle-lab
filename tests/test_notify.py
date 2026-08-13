import json
from unittest.mock import patch

from btc_puzzle_lab.audit import AuditResult
from btc_puzzle_lab.hits import Hit
from btc_puzzle_lab.notify import (
    _webhook_payload,
    build_hit_embed,
    build_hit_message,
    notify_hit,
)
from btc_puzzle_lab.paths import clear_path_cache
from btc_puzzle_lab.settings import (
    NotifySettings,
    RelaySettings,
    get_notify_settings,
    validate_notify_settings,
    validate_relay_settings,
)
from btc_puzzle_lab.transfer import TransferResult


def _hit() -> Hit:
    return Hit(
        puzzle_id=71,
        address="1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU",
        private_key_hex="a" * 64,
        engine="bitcrack",
        found_at="2026-08-09T00:00:00+00:00",
        verified=True,
    )


def test_build_hit_message_has_no_private_key():
    text = build_hit_message(_hit())
    assert "puzzle=#71" in text
    assert "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU" in text
    assert "a" * 64 not in text
    assert "private key kept in local" in text


def test_hit_embed_carries_no_private_key():
    embed = json.dumps(build_hit_embed(_hit()))
    assert "a" * 64 not in embed
    assert "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU" in embed
    assert "Puzzle #71 solved" in embed


def test_hit_embed_colour_tracks_transfer_status():
    green = build_hit_embed(_hit(), transfer=TransferResult("broadcast", "sent"))["color"]
    amber = build_hit_embed(_hit(), transfer=TransferResult("skipped", "gated"))["color"]
    assert green != amber
    # A failed audit outranks whatever the sweep reported.
    red = build_hit_embed(
        _hit(),
        audit=AuditResult(_hit(), address_ok=False, derived_address="x", balance_sats=None),
        transfer=TransferResult("broadcast", "sent"),
    )["color"]
    assert red not in (green, amber)


def test_discord_payload_uses_embed_not_content():
    embed = build_hit_embed(_hit())
    payload = _webhook_payload("plain", "https://discord.com/api/webhooks/1/x", embed)
    assert "embeds" in payload and "content" not in payload
    # Non-Discord endpoints keep the plain-text shape.
    slack = _webhook_payload("plain", "https://hooks.slack.com/services/x", embed)
    assert slack == {"text": "plain"}


def test_validate_notify_requires_channel_when_enabled():
    errors = validate_notify_settings(
        NotifySettings(enabled=True, webhook_url="", telegram_bot_token="", telegram_chat_id="")
    )
    assert errors
    assert validate_notify_settings(
        NotifySettings(
            enabled=True,
            webhook_url="https://example.com/hook",
            telegram_bot_token="",
            telegram_chat_id="",
        )
    ) == []


def test_validate_relay_requires_token_when_url_is_set():
    errors = validate_relay_settings(
        RelaySettings(
            url="https://control.example:8787/hit",
            seal_pubkey="ab" * 32,
            token="",
        )
    )
    assert any("RELAY_TOKEN" in err for err in errors)


def test_notify_hit_posts_webhook(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setenv("NOTIFY_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_WEBHOOK_URL", "https://example.com/hook")
    settings = get_notify_settings()
    assert settings.enabled and settings.configured

    with patch("btc_puzzle_lab.notify.requests.post") as post:
        post.return_value.status_code = 204
        post.return_value.text = ""
        results = notify_hit(_hit(), settings=settings)
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].channel == "webhook"
    sent = post.call_args.kwargs.get("json") or {}
    blob = str(sent)
    assert "puzzle=#71" in blob or "1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU" in blob
    assert "a" * 64 not in blob


def test_notify_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    monkeypatch.setenv("NOTIFY_ENABLED", "false")
    with patch("btc_puzzle_lab.notify.requests.post") as post:
        results = notify_hit(_hit())
    assert results[0].ok is True
    assert "NOTIFY_ENABLED=false" in results[0].message
    post.assert_not_called()
