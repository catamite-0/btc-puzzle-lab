import json
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

from btc_puzzle_lab.cli import main
from btc_puzzle_lab.hits import Hit, read_hits
from btc_puzzle_lab.hub import ingest_sealed_hit, make_hub_server, parse_ingest_body
from btc_puzzle_lab.notify import notify_hit
from btc_puzzle_lab.relay import generate_relay_keypair, seal_hit, write_relay_secret
from btc_puzzle_lab.settings import NotifySettings, bootstrap_config, get_transfer_settings
from btc_puzzle_lab.transfer import TransferResult

_PRACTICE_ADDR = "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"
_TOKEN = "control-hub-token-1"


def _practice_hit() -> Hit:
    return Hit(
        puzzle_id=1,
        address=_PRACTICE_ADDR,
        private_key_hex="1",
        engine="sequential",
        found_at="2026-08-09T00:00:00+00:00",
        verified=True,
    )


def _bad_key_hit() -> Hit:
    return Hit(
        puzzle_id=71,
        address="1PWo3JeB9jrGwfHDNpdGK54CRas7fsVzXU",
        private_key_hex="a" * 64,
        engine="bitcrack",
        found_at="2026-08-09T00:00:00+00:00",
        verified=True,
    )


def _payload(hit: Hit, pub: str, **extra):
    body = {"sealed": seal_hit(hit, pub), "puzzle_id": hit.puzzle_id, "address": hit.address}
    body.update(extra)
    return body


def test_ingest_records_hit_and_skips_duplicate_sweep():
    secret, pub = generate_relay_keypair()
    write_relay_secret(secret)
    hit = _practice_hit()
    sweeps: list[Hit] = []
    notify_kwargs: list[dict] = []

    def sweep_fn(item: Hit) -> TransferResult:
        sweeps.append(item)
        return TransferResult(status="dry_run", message="mocked")

    def notify_fn(item: Hit, **kwargs):
        notify_kwargs.append(kwargs)
        return []

    payload = _payload(hit, pub)
    first = ingest_sealed_hit(payload, sweep_fn=sweep_fn, notify_fn=notify_fn)
    assert first.status == "accepted"
    assert first.ok is True
    assert first.audit_ok is True
    assert first.swept == "dry_run"
    stored = read_hits()
    assert len(stored) == 1
    assert stored[0].private_key_hex.lower().endswith("1")
    assert "skip_relay" not in notify_kwargs[0]

    second = ingest_sealed_hit(payload, sweep_fn=sweep_fn, notify_fn=notify_fn)
    assert second.status == "duplicate"
    assert second.ok is True
    assert len(sweeps) == 1
    assert len(notify_kwargs) == 1


def test_ingest_rejects_puzzle_id_mismatch():
    secret, pub = generate_relay_keypair()
    write_relay_secret(secret)
    payload = _payload(_practice_hit(), pub, puzzle_id=99)
    result = ingest_sealed_hit(payload, sweep=False, notify=False)
    assert result.ok is False
    assert result.status == "rejected"
    assert "mismatch" in result.message
    assert read_hits() == []


def test_ingest_skips_sweep_when_audit_fails():
    secret, pub = generate_relay_keypair()
    write_relay_secret(secret)
    sweeps: list[Hit] = []

    def sweep_fn(item: Hit) -> TransferResult:
        sweeps.append(item)
        return TransferResult(status="dry_run", message="mocked")

    result = ingest_sealed_hit(
        _payload(_bad_key_hit(), pub),
        sweep_fn=sweep_fn,
        notify=False,
    )
    assert result.status == "accepted"
    assert result.audit_ok is False
    assert result.swept == "skipped"
    assert sweeps == []
    assert read_hits()[0].verified is False


def test_notify_hit_does_not_post_to_a_relay_url():
    settings = NotifySettings(
        enabled=True,
        webhook_url="https://discord.com/api/webhooks/1/x",
        telegram_bot_token="",
        telegram_chat_id="",
    )
    with patch("btc_puzzle_lab.notify.requests.post") as post:
        post.return_value.status_code = 204
        post.return_value.text = ""
        results = notify_hit(_bad_key_hit(), settings=settings)
    assert all(item.channel != "relay" for item in results)
    assert results[0].channel == "webhook"
    assert post.call_count == 1
    assert "discord.com" in str(post.call_args)


def test_parse_ingest_body_json_and_form():
    raw = json.dumps({"sealed": "bpl1.abc"}).encode()
    assert parse_ingest_body(raw, "application/json")["sealed"] == "bpl1.abc"
    form = parse_ingest_body(b"desp=hello+bpl1.token", "application/x-www-form-urlencoded")
    assert "desp" in form


def test_hub_http_requires_bearer_and_records_hit():
    secret, pub = generate_relay_keypair()
    write_relay_secret(secret)
    httpd = make_hub_server(
        ("127.0.0.1", 0),
        relay_token=_TOKEN,
        sweep=False,
        notify=False,
        secret=secret,
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _host, port = httpd.server_address
    try:
        deadline = time.time() + 2
        health = f"http://127.0.0.1:{port}/health"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(health, timeout=0.2) as resp:
                    assert json.loads(resp.read())["ok"] is True
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("hub health check never came up")

        body = json.dumps(_payload(_practice_hit(), pub)).encode()
        hit_url = f"http://127.0.0.1:{port}/hit"
        req = urllib.request.Request(
            hit_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected 401 without bearer")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        req2 = urllib.request.Request(
            hit_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_TOKEN}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req2, timeout=5) as resp:
            payload = json.loads(resp.read())
        assert payload["ok"] is True
        assert payload["status"] == "accepted"
        assert payload["puzzle_id"] == 1
        dumped = json.dumps(payload)
        assert "private" not in dumped.lower()
        assert "0000000000000000000000000000000000000000000000000000000000000001" not in dumped
        assert read_hits()[0].puzzle_id == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_hub_cli_requires_token(capsys):
    assert main(["hub", "--no-sweep"]) == 2
    assert "RELAY_TOKEN" in capsys.readouterr().err


def test_hub_cli_requires_secret(monkeypatch, capsys):
    monkeypatch.setenv("RELAY_TOKEN", _TOKEN)
    assert main(["hub", "--no-sweep"]) == 2
    err = capsys.readouterr().err
    assert "relay-keygen" in err


def test_relay_without_dest_does_not_enable_transfer():
    _, pub = generate_relay_keypair()
    update = bootstrap_config(
        relay_url="https://control.example:8787/hit",
        relay_seal_pubkey=pub,
        relay_token=_TOKEN,
    )
    assert get_transfer_settings().enabled is False
    assert "relay=set" in update.format()
    assert _TOKEN not in update.format()


def test_config_new_relay_token_prints_once(capsys):
    assert main(["config", "--new-relay-token"]) == 0
    out = capsys.readouterr().out
    assert "relay token :" in out
    shown = [
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("relay token :")
    ]
    assert shown and len(shown[0]) >= 16
    assert main(["config"]) == 0
    again = capsys.readouterr().out
    assert shown[0] not in again
