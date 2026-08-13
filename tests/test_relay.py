from unittest.mock import patch

from btc_puzzle_lab.cli import main
from btc_puzzle_lab.hits import Hit
from btc_puzzle_lab.notify import notify_hit
from btc_puzzle_lab.relay import (
    build_relay_message,
    deliver_relay,
    extract_seal_token,
    generate_relay_keypair,
    seal_hit,
    unseal_hit,
    write_relay_secret,
)
from btc_puzzle_lab.settings import NotifySettings, bootstrap_config


def _hit() -> Hit:
    return Hit(
        puzzle_id=71,
        address="1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU",
        private_key_hex="a" * 64,
        engine="bitcrack",
        found_at="2026-08-09T00:00:00+00:00",
        verified=True,
    )


def test_seal_roundtrip_does_not_embed_plaintext_key():
    secret, pub = generate_relay_keypair()
    hit = _hit()
    token = seal_hit(hit, pub)
    assert "a" * 64 not in token
    opened = unseal_hit(token, secret)
    assert opened.puzzle_id == 71
    assert opened.address == hit.address
    assert opened.private_key_hex == hit.private_key_hex


def test_relay_message_has_no_private_key():
    secret, pub = generate_relay_keypair()
    hit = _hit()
    text = build_relay_message(hit, sealed=seal_hit(hit, pub))
    assert "a" * 64 not in text
    assert "sealed=bpl1." in text
    token = extract_seal_token(text)
    assert unseal_hit(token, secret).puzzle_id == 71


def test_unseal_cli_hides_key_without_show_key(capsys):
    secret, pub = generate_relay_keypair()
    write_relay_secret(secret)
    token = seal_hit(_hit(), pub)
    assert main(["unseal", token]) == 0
    out = capsys.readouterr().out
    assert "puzzle  : #71" in out
    assert "a" * 64 not in out
    assert main(["unseal", token, "--show-key"]) == 0
    shown = capsys.readouterr().out
    assert "a" * 64 in shown


def test_notify_hit_posts_relay_when_discord_disabled():
    secret, pub = generate_relay_keypair()
    settings = NotifySettings(
        enabled=False,
        webhook_url="",
        telegram_bot_token="",
        telegram_chat_id="",
        relay_url="https://sctapi.ftqq.com/demo.send",
        relay_seal_pubkey=pub,
    )
    with patch("btc_puzzle_lab.relay.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.text = "ok"
        results = notify_hit(_hit(), settings=settings)
    assert results[0].channel == "relay"
    assert results[0].ok is True
    sent = post.call_args.kwargs.get("data") or {}
    blob = str(sent)
    assert "a" * 64 not in blob
    assert "bpl1." in blob
    opened = unseal_hit(extract_seal_token(blob), secret)
    assert opened.private_key_hex == "a" * 64


def test_deliver_relay_writes_outbox_without_key(tmp_path):
    _secret, pub = generate_relay_keypair()
    with patch("btc_puzzle_lab.relay.requests.post") as post:
        post.return_value.status_code = 500
        post.return_value.text = "down"
        result = deliver_relay(
            _hit(),
            url="https://example.com/relay",
            seal_pubkey=pub,
        )
    assert result.ok is False
    outbox = (tmp_path / "state" / "relay_outbox.jsonl").read_text(encoding="utf-8")
    assert "a" * 64 not in outbox
    assert "bpl1." in outbox


def test_deliver_relay_sends_bearer_and_omits_key(monkeypatch):
    monkeypatch.setenv("RELAY_TOKEN", "control-hub-token-1")
    _secret, pub = generate_relay_keypair()
    with patch("btc_puzzle_lab.relay.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.text = "ok"
        result = deliver_relay(
            _hit(),
            url="https://control.example:8787/hit",
            seal_pubkey=pub,
        )
    assert result.ok is True
    headers = post.call_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer control-hub-token-1"
    payload = post.call_args.kwargs.get("json") or {}
    blob = str(payload)
    assert "a" * 64 not in blob
    assert payload.get("puzzle_id") == 71
    assert isinstance(payload.get("sealed"), str) and payload["sealed"].startswith("bpl1.")


def test_config_relay_without_dest():
    _, pub = generate_relay_keypair()
    update = bootstrap_config(
        relay_url="https://control.example:8787/hit",
        relay_seal_pubkey=pub,
        relay_token="control-hub-token-1",
    )
    assert "relay=set" in update.format()
    assert "control-hub-token-1" not in update.format()
