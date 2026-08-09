from unittest.mock import patch

from btc_puzzle_lab.hits import Hit
from btc_puzzle_lab.notify import build_hit_message, notify_hit
from btc_puzzle_lab.paths import clear_path_cache
from btc_puzzle_lab.settings import NotifySettings, get_notify_settings, validate_notify_settings


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
