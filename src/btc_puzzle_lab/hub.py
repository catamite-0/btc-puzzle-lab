"""Always-on control VPS: receive sealed hits, unseal, notify, sweep.

Hunt boxes only search and POST ciphertext. This process holds
``config/relay-secret``, Discord/Telegram, and the sweep dest.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

from btc_puzzle_lab.audit import AuditResult, verify_hit
from btc_puzzle_lab.hits import Hit, append_hit, utc_now
from btc_puzzle_lab.notify import NotifyResult, notify_hit
from btc_puzzle_lab.relay import extract_seal_token, load_relay_secret, unseal_hit
from btc_puzzle_lab.runlog import log_event
from btc_puzzle_lab.settings import (
    get_transfer_settings,
    load_dotenv_files,
    validate_transfer_settings,
)
from btc_puzzle_lab.transfer import TransferResult, sweep_hit

MIN_RELAY_TOKEN_LEN = 16
MAX_BODY_BYTES = 1_000_000
DEFAULT_HUB_HOST = "0.0.0.0"
DEFAULT_HUB_PORT = 8787

_SEAL_KEYS = ("sealed", "token")
_TEXT_KEYS = ("message", "body", "desp", "content", "text", "alert")


@dataclass(frozen=True)
class IngestResult:
    ok: bool
    status: str
    puzzle_id: int | None = None
    address: str = ""
    audit_ok: bool = False
    duplicate: bool = False
    swept: str = ""
    message: str = ""

    def as_public_dict(self) -> dict[str, Any]:
        """JSON for the hunt box — never includes a private key."""
        return {
            "ok": self.ok,
            "status": self.status,
            "puzzle_id": self.puzzle_id,
            "address": self.address,
            "audit_ok": self.audit_ok,
            "duplicate": self.duplicate,
            "swept": self.swept,
            "message": self.message,
        }


def bearer_token_ok(got: str, expected: str) -> bool:
    if not expected:
        return False
    got_b = got.encode("utf-8")
    exp_b = expected.encode("utf-8")
    if len(got_b) != len(exp_b):
        secrets.compare_digest(exp_b, exp_b)
        return False
    return secrets.compare_digest(got_b, exp_b)


def request_bearer(headers: Any) -> str:
    raw = headers.get("Authorization", "") if headers is not None else ""
    if isinstance(raw, str) and raw.lower().startswith("bearer "):
        return raw[7:].strip()
    extra = headers.get("X-Relay-Token", "") if headers is not None else ""
    return extra.strip() if isinstance(extra, str) else ""


def parse_ingest_body(raw: bytes, content_type: str = "") -> dict[str, Any]:
    text = raw.decode("utf-8")
    if not text.strip():
        raise ValueError("empty body")
    ct = (content_type or "").lower()
    if "json" in ct or text.lstrip().startswith(("{", "[")):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data
    if "x-www-form-urlencoded" in ct:
        parsed = parse_qs(text, keep_blank_values=True)
        return {key: (vals[0] if vals else "") for key, vals in parsed.items()}
    return {"text": text}


def extract_sealed_from_payload(payload: dict[str, Any]) -> str:
    for key in _SEAL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return extract_seal_token(value)
    alert = payload.get("alert")
    if isinstance(alert, dict):
        return extract_sealed_from_payload(alert)
    for key in _TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and "bpl1." in value:
            return extract_seal_token(value)
    raise ValueError("no sealed token in payload")


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def ingest_sealed_hit(
    payload: dict[str, Any],
    *,
    sweep: bool = True,
    notify: bool = True,
    secret: str | None = None,
    sweep_fn: Callable[[Hit], TransferResult] | None = None,
    notify_fn: Callable[..., list[NotifyResult]] | None = None,
) -> IngestResult:
    """Unseal a hunt POST, record the hit, notify (no relay loop), optional sweep."""
    try:
        sealed = extract_sealed_from_payload(payload)
        opened = unseal_hit(sealed, secret)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        log_event("hub_ingest", status="rejected", error="unseal")
        return IngestResult(
            ok=False,
            status="rejected",
            message="could not unseal" if "unseal" in str(exc).lower() or "bpl1" in str(exc) else str(exc)[:120],
        )

    try:
        outer_id = _optional_int(payload.get("puzzle_id"))
    except (TypeError, ValueError):
        return IngestResult(ok=False, status="rejected", message="puzzle_id is not an int")
    if outer_id is not None and outer_id != opened.puzzle_id:
        log_event(
            "hub_ingest",
            status="rejected",
            error="puzzle_id mismatch",
            puzzle_id=opened.puzzle_id,
        )
        return IngestResult(
            ok=False,
            status="rejected",
            puzzle_id=opened.puzzle_id,
            address=opened.address,
            message="puzzle_id mismatch (sealed payload is authoritative)",
        )

    outer_addr = payload.get("address")
    if isinstance(outer_addr, str) and outer_addr.strip() and outer_addr.strip() != opened.address:
        log_event(
            "hub_ingest",
            status="rejected",
            error="address mismatch",
            puzzle_id=opened.puzzle_id,
        )
        return IngestResult(
            ok=False,
            status="rejected",
            puzzle_id=opened.puzzle_id,
            address=opened.address,
            message="address mismatch (sealed payload is authoritative)",
        )

    hit = Hit(
        puzzle_id=opened.puzzle_id,
        address=opened.address,
        private_key_hex=opened.private_key_hex,
        engine=opened.engine or "relay",
        found_at=opened.found_at or utc_now(),
        verified=False,
    )
    audit: AuditResult = verify_hit(hit)
    audit_ok = bool(audit.address_ok and not audit.error)
    hit = Hit(
        puzzle_id=hit.puzzle_id,
        address=hit.address,
        private_key_hex=hit.private_key_hex,
        engine=hit.engine,
        found_at=hit.found_at,
        verified=audit_ok,
    )
    recorded = append_hit(hit)
    if recorded.duplicate:
        log_event(
            "hub_ingest",
            status="duplicate",
            puzzle_id=hit.puzzle_id,
            address=hit.address,
        )
        return IngestResult(
            ok=True,
            status="duplicate",
            puzzle_id=hit.puzzle_id,
            address=hit.address,
            audit_ok=audit_ok,
            duplicate=True,
            message="already recorded",
        )

    transfer: TransferResult | None = None
    swept = ""
    if sweep and audit_ok:
        runner = sweep_fn or sweep_hit
        transfer = runner(hit)
        swept = transfer.status
    elif sweep and not audit_ok:
        swept = "skipped"

    if notify:
        sender = notify_fn or notify_hit
        sender(hit, audit=audit, transfer=transfer, skip_relay=True)

    log_event(
        "hub_ingest",
        status="accepted",
        puzzle_id=hit.puzzle_id,
        address=hit.address,
        audit_ok=audit_ok,
        swept=swept or None,
    )
    return IngestResult(
        ok=True,
        status="accepted",
        puzzle_id=hit.puzzle_id,
        address=hit.address,
        audit_ok=audit_ok,
        duplicate=False,
        swept=swept,
        message="recorded" if audit_ok else "recorded (audit failed; sweep skipped)",
    )


def hub_preflight(*, sweep: bool = True) -> str:
    load_dotenv_files()
    token = os.getenv("RELAY_TOKEN", "").strip()
    if len(token) < MIN_RELAY_TOKEN_LEN:
        raise ValueError(
            "set a shared RELAY_TOKEN (16+ chars) on the control VPS: "
            "btc-puzzle-lab config --new-relay-token"
        )
    try:
        load_relay_secret()
    except FileNotFoundError as exc:
        raise ValueError(
            f"{exc}. run `btc-puzzle-lab relay-keygen` on this control host "
            "(do not copy config/relay-secret onto hunt boxes)"
        ) from exc
    if sweep:
        settings = get_transfer_settings()
        if not settings.enabled or not settings.dest_addr:
            raise ValueError(
                "control VPS needs a sweep dest: btc-puzzle-lab config --dest <addr> "
                "(or pass --no-sweep)"
            )
        errors = validate_transfer_settings(settings)
        if errors:
            raise ValueError("; ".join(errors))
    return token


class HubHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        token: str,
        *,
        sweep: bool = True,
        notify: bool = True,
        secret: str | None = None,
    ) -> None:
        self.relay_token = token
        self.sweep = sweep
        self.notify = notify
        self.secret = secret
        super().__init__(server_address, HubHandler)


class HubHandler(BaseHTTPRequestHandler):
    server_version = "btc-puzzle-lab-hub/1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log_event("hub_http", message=(fmt % args)[:200])

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/"}:
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.split("?", 1)[0]
        if path not in {"/hit", "/relay"}:
            self._json(404, {"ok": False, "error": "not found"})
            return
        server = self.server
        assert isinstance(server, HubHTTPServer)
        got = request_bearer(self.headers)
        if not bearer_token_ok(got, server.relay_token):
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        raw_len = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_len)
        except ValueError:
            self._json(400, {"ok": False, "error": "bad content-length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = parse_ingest_body(raw, self.headers.get("Content-Type", ""))
            result = ingest_sealed_hit(
                payload,
                sweep=server.sweep,
                notify=server.notify,
                secret=server.secret,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"ok": False, "error": str(exc)[:120]})
            return
        except Exception:  # noqa: BLE001 — never leak internals (or keys) to the hunt box
            log_event("hub_http", status="error", error="ingest failed")
            self._json(500, {"ok": False, "error": "ingest failed"})
            return
        code = 200 if result.ok else 400
        self._json(code, result.as_public_dict())


def make_hub_server(
    addr: tuple[str, int],
    *,
    relay_token: str,
    sweep: bool = True,
    notify: bool = True,
    secret: str | None = None,
) -> HubHTTPServer:
    if len(relay_token) < MIN_RELAY_TOKEN_LEN:
        raise ValueError(f"RELAY_TOKEN must be at least {MIN_RELAY_TOKEN_LEN} characters")
    return HubHTTPServer(addr, relay_token, sweep=sweep, notify=notify, secret=secret)


def serve_hub(
    *,
    host: str = DEFAULT_HUB_HOST,
    port: int = DEFAULT_HUB_PORT,
    sweep: bool = True,
    notify: bool = True,
) -> None:
    token = hub_preflight(sweep=sweep)
    httpd = make_hub_server((host, port), relay_token=token, sweep=sweep, notify=notify)
    bound_host, bound_port = httpd.server_address
    print(f"hub listening on http://{bound_host}:{bound_port}/hit")
    print("POST sealed JSON with Authorization: Bearer <RELAY_TOKEN>")
    print("GET  /health")
    if host in {"0.0.0.0", "::", "[::]"}:
        print("bind is public; put TLS (caddy/nginx) in front and firewall the port")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
