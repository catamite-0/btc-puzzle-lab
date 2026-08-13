from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from btc_puzzle_lab.crypto import is_valid_btc_address
from btc_puzzle_lab.paths import ENV_FILE, REPO_ROOT
from btc_puzzle_lab.relay import is_seal_pubkey

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
    )


def validate_notify_settings(settings: NotifySettings) -> list[str]:
    errors: list[str] = []
    if settings.relay_url:
        parsed = settings.relay_url.lower()
        if not (parsed.startswith("https://") or parsed.startswith("http://")):
            errors.append("RELAY_URL must be an http(s) URL")
        if settings.relay_seal_pubkey and not is_seal_pubkey(settings.relay_seal_pubkey):
            errors.append("RELAY_SEAL_PUBKEY must be 32-byte X25519 pubkey hex")
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
    return f"enabled={cfg.enabled} channels={ch}"


def ensure_config_dir() -> Path:
    from btc_puzzle_lab.paths import CONFIG_DIR

    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return CONFIG_DIR
