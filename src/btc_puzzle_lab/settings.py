from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from btc_puzzle_lab.crypto import is_valid_btc_address
from btc_puzzle_lab.paths import ENV_FILE, REPO_ROOT

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC"
FEE_STRATEGIES = ("economy", "normal", "priority")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else int(raw)
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass(frozen=True)
class TransferSettings:
    enabled: bool
    dry_run: bool
    dest_addr: str
    live_confirm: str
    min_balance_sats: int
    min_send_sats: int
    default_fee_rate: int
    max_fee_rate: int
    fee_strategy: str = "normal"
    fee_target_blocks: int = 2
    rbf: bool = True
    confirmed_only: bool = True
    max_fee_sats: int = 100_000

    @property
    def live_ok(self) -> bool:
        return self.live_confirm == LIVE_CONFIRM_PHRASE


@dataclass(frozen=True)
class NotifySettings:
    enabled: bool
    webhook_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    relay_url: str = ""
    relay_seal_pubkey: str = ""
    relay_token: str = ""

    @property
    def configured(self) -> bool:
        if self.webhook_url:
            return True
        if self.relay_url:
            return True
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_dotenv_files() -> None:
    # Optional local config; never required for search/audit-only use.
    engines_env = REPO_ROOT / "config" / "engines.env"
    if engines_env.is_file():
        load_dotenv(engines_env, override=False)
    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE, override=False)
    root_env = REPO_ROOT / ".env"
    if root_env.is_file():
        load_dotenv(root_env, override=False)


def get_transfer_settings() -> TransferSettings:
    load_dotenv_files()
    strategy = os.getenv("AUTO_TRANSFER_FEE_STRATEGY", "normal").strip().lower() or "normal"
    settings = TransferSettings(
        enabled=_env_bool("AUTO_TRANSFER_ENABLED", False),
        dry_run=_env_bool("AUTO_TRANSFER_DRY_RUN", True),
        dest_addr=os.getenv("AUTO_TRANSFER_DEST_ADDR", "").strip(),
        live_confirm=os.getenv("AUTO_TRANSFER_LIVE_CONFIRM", "").strip(),
        min_balance_sats=_env_int("AUTO_TRANSFER_MIN_BALANCE_SATS", 5000, minimum=0),
        min_send_sats=_env_int("AUTO_TRANSFER_MIN_SEND_SATS", 546, minimum=0),
        default_fee_rate=_env_int("AUTO_TRANSFER_DEFAULT_FEE_RATE", 15, minimum=1),
        max_fee_rate=_env_int("AUTO_TRANSFER_MAX_FEE_RATE", 250, minimum=1),
        fee_strategy=strategy,
        fee_target_blocks=_env_int("AUTO_TRANSFER_FEE_TARGET_BLOCKS", 2, minimum=1),
        rbf=_env_bool("AUTO_TRANSFER_RBF", True),
        confirmed_only=_env_bool("AUTO_TRANSFER_CONFIRMED_ONLY", True),
        max_fee_sats=_env_int("AUTO_TRANSFER_MAX_FEE_SATS", 100_000, minimum=1),
    )
    if settings.default_fee_rate > settings.max_fee_rate:
        raise ValueError("AUTO_TRANSFER_DEFAULT_FEE_RATE exceeds AUTO_TRANSFER_MAX_FEE_RATE")
    return settings


def validate_transfer_settings(settings: TransferSettings) -> list[str]:
    errors: list[str] = []
    if not settings.enabled:
        return errors
    if not settings.dest_addr:
        errors.append("AUTO_TRANSFER_ENABLED but AUTO_TRANSFER_DEST_ADDR is empty")
    elif not is_valid_btc_address(settings.dest_addr):
        errors.append("AUTO_TRANSFER_DEST_ADDR is not a valid BTC address")
    if settings.fee_strategy not in FEE_STRATEGIES:
        errors.append(
            f"AUTO_TRANSFER_FEE_STRATEGY must be one of {', '.join(FEE_STRATEGIES)}"
        )
    if not settings.dry_run and not settings.live_ok:
        errors.append(
            "live transfer requires AUTO_TRANSFER_LIVE_CONFIRM="
            f"{LIVE_CONFIRM_PHRASE}"
        )
    return errors


def get_notify_settings() -> NotifySettings:
    load_dotenv_files()
    return NotifySettings(
        enabled=_env_bool("NOTIFY_ENABLED", False),
        webhook_url=os.getenv("NOTIFY_WEBHOOK_URL", "").strip(),
        telegram_bot_token=os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("NOTIFY_TELEGRAM_CHAT_ID", "").strip(),
        relay_url=os.getenv("RELAY_URL", "").strip(),
        relay_seal_pubkey=os.getenv("RELAY_SEAL_PUBKEY", "").strip(),
        relay_token=os.getenv("RELAY_TOKEN", "").strip(),
    )


def validate_notify_settings(settings: NotifySettings) -> list[str]:
    errors: list[str] = []
    if settings.relay_url:
        parsed = settings.relay_url.lower()
        if not (parsed.startswith("https://") or parsed.startswith("http://")):
            errors.append("RELAY_URL must be an http(s) URL")
        if settings.relay_seal_pubkey:
            from btc_puzzle_lab.relay import is_seal_pubkey

            if not is_seal_pubkey(settings.relay_seal_pubkey):
                errors.append("RELAY_SEAL_PUBKEY must be 32-byte X25519 pubkey hex")
        if settings.relay_token and len(settings.relay_token) < 16:
            errors.append("RELAY_TOKEN must be at least 16 characters")
    elif settings.relay_seal_pubkey:
        errors.append("RELAY_SEAL_PUBKEY set but RELAY_URL is empty")
    if not settings.enabled:
        return errors
    if not settings.configured:
        errors.append(
            "NOTIFY_ENABLED but neither NOTIFY_WEBHOOK_URL, Telegram, "
            "nor RELAY_URL is set"
        )
    if settings.webhook_url:
        parsed = settings.webhook_url.lower()
        if not (parsed.startswith("https://") or parsed.startswith("http://")):
            errors.append("NOTIFY_WEBHOOK_URL must be an http(s) URL")
    if settings.telegram_bot_token and not settings.telegram_chat_id:
        errors.append("NOTIFY_TELEGRAM_BOT_TOKEN set but NOTIFY_TELEGRAM_CHAT_ID empty")
    if settings.telegram_chat_id and not settings.telegram_bot_token:
        errors.append("NOTIFY_TELEGRAM_CHAT_ID set but NOTIFY_TELEGRAM_BOT_TOKEN empty")
    return errors


def format_notify_policy(settings: NotifySettings | None = None) -> str:
    cfg = settings or get_notify_settings()
    channels: list[str] = []
    if cfg.webhook_url:
        channels.append("webhook")
    if cfg.telegram_bot_token and cfg.telegram_chat_id:
        channels.append("telegram")
    if cfg.relay_url:
        channels.append("relay+seal" if cfg.relay_seal_pubkey else "relay")
    ch = ",".join(channels) if channels else "(none)"
    token = "set" if cfg.relay_token else "unset"
    return f"enabled={cfg.enabled} channels={ch} relay_token={token}"


def ensure_config_dir() -> Path:
    from btc_puzzle_lab.paths import CONFIG_DIR

    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return CONFIG_DIR


_ENV_HEADER = (
    "# btc-puzzle-lab local config. Never commit this file.",
    "# Written by `btc-puzzle-lab auto` / `config`; hand edits are preserved.",
    "# Full option list with comments: config/.env.example",
)


def write_env_values(values: dict[str, str], path: Path | None = None) -> Path:
    """Merge ``KEY=value`` pairs into ``config/.env`` (mode 0600).

    Existing lines are rewritten in place so hand-written settings, ordering and
    comments survive; anything new is appended. The file holds a sweep destination,
    so it is created 0600 and never widened afterwards.
    """
    ensure_config_dir()
    target = Path(path) if path is not None else Path(ENV_FILE)
    existing = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    if not existing:
        existing = list(_ENV_HEADER)

    remaining = dict(values)
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.extend(f"{key}={value}" for key, value in remaining.items())

    fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).rstrip("\n") + "\n")
    os.chmod(target, 0o600)
    return target


@dataclass(frozen=True)
class ConfigUpdate:
    path: Path
    keys: tuple[str, ...]
    dest_addr: str
    notify_channels: tuple[str, ...]
    live: bool

    def format(self) -> str:
        if not self.keys:
            return "config unchanged (using existing config/.env)"
        mode = "LIVE BROADCAST" if self.live else "dry-run"
        bits = [f"wrote {len(self.keys)} key(s) to {self.path}"]
        if self.dest_addr:
            bits.append(f"sweep dest={self.dest_addr} mode={mode}")
        if self.notify_channels:
            bits.append(f"notify={','.join(self.notify_channels)}")
        if any(key.startswith("RELAY_") for key in self.keys):
            bits.append("relay=set")
        return "; ".join(bits)


def bootstrap_config(
    *,
    dest_addr: str | None = None,
    notify_url: str | None = None,
    telegram_token: str | None = None,
    telegram_chat: str | None = None,
    live: bool = False,
    relay_url: str | None = None,
    relay_seal_pubkey: str | None = None,
    relay_token: str | None = None,
    path: Path | None = None,
) -> ConfigUpdate:
    """Persist payout, alert, and optional hunt-to-hub relay settings.

    Enabling a sweep destination turns auto-transfer on in **dry-run**: a hit is
    signed and written to ``state/dryrun_*.txhex`` but nothing is broadcast. Live
    broadcast is a separate, explicit decision (``live=True``) because it moves
    real BTC, and it writes the confirm phrase the transfer layer demands.

    Hunt boxes can persist ``RELAY_URL`` / pubkey / token without a dest; the
    control VPS that runs ``hub`` holds dest and ``config/relay-secret``.
    """
    load_dotenv_files()
    values: dict[str, str] = {}
    channels: list[str] = []

    if dest_addr:
        dest_addr = dest_addr.strip()
        if not is_valid_btc_address(dest_addr):
            raise ValueError(f"not a valid BTC address: {dest_addr}")
        values["AUTO_TRANSFER_DEST_ADDR"] = dest_addr
        values["AUTO_TRANSFER_ENABLED"] = "true"
        values["AUTO_TRANSFER_DRY_RUN"] = "false" if live else "true"
        if live:
            values["AUTO_TRANSFER_LIVE_CONFIRM"] = LIVE_CONFIRM_PHRASE
    elif live:
        raise ValueError("--live needs a sweep destination (--dest)")

    if notify_url:
        notify_url = notify_url.strip()
        if not notify_url.lower().startswith(("http://", "https://")):
            raise ValueError("notify URL must be an http(s) URL")
        values["NOTIFY_WEBHOOK_URL"] = notify_url
        values["NOTIFY_ENABLED"] = "true"
        channels.append("webhook")

    if telegram_token or telegram_chat:
        if not (telegram_token and telegram_chat):
            raise ValueError("Telegram needs both a bot token and a chat id")
        values["NOTIFY_TELEGRAM_BOT_TOKEN"] = telegram_token.strip()
        values["NOTIFY_TELEGRAM_CHAT_ID"] = telegram_chat.strip()
        values["NOTIFY_ENABLED"] = "true"
        channels.append("telegram")

    if relay_url is not None:
        relay_url = relay_url.strip()
        if not relay_url.lower().startswith(("http://", "https://")):
            raise ValueError("relay URL must be an http(s) URL")
        values["RELAY_URL"] = relay_url
        channels.append("relay")
    if relay_seal_pubkey is not None:
        from btc_puzzle_lab.relay import is_seal_pubkey

        relay_seal_pubkey = relay_seal_pubkey.strip()
        if not is_seal_pubkey(relay_seal_pubkey):
            raise ValueError("RELAY_SEAL_PUBKEY must be 32-byte X25519 pubkey hex")
        values["RELAY_SEAL_PUBKEY"] = relay_seal_pubkey
    if relay_token is not None:
        relay_token = relay_token.strip()
        if len(relay_token) < 16:
            raise ValueError("RELAY_TOKEN must be at least 16 characters")
        values["RELAY_TOKEN"] = relay_token

    effective_relay = values.get("RELAY_URL") or os.getenv("RELAY_URL", "").strip()
    effective_pub = values.get("RELAY_SEAL_PUBKEY") or os.getenv("RELAY_SEAL_PUBKEY", "").strip()
    if effective_relay and not effective_pub:
        raise ValueError(
            "--relay needs --relay-seal-pubkey from `relay-keygen` on the control VPS"
        )
    if effective_pub and not effective_relay:
        raise ValueError("relay seal pubkey set but --relay URL is empty")

    if not values:
        return ConfigUpdate(
            path=Path(path) if path is not None else Path(ENV_FILE),
            keys=(),
            dest_addr="",
            notify_channels=(),
            live=False,
        )

    target = write_env_values(values, path=path)
    os.environ.update(values)
    return ConfigUpdate(
        path=target,
        keys=tuple(sorted(values)),
        dest_addr=values.get("AUTO_TRANSFER_DEST_ADDR", ""),
        notify_channels=tuple(channels),
        live=live,
    )
