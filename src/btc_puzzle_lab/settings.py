from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from btc_puzzle_lab.crypto import is_valid_btc_address
from btc_puzzle_lab.paths import ENV_FILE, REPO_ROOT

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC"
FEE_STRATEGIES = ("economy", "normal", "priority")


def _value(name: str, values: Mapping[str, str | None]) -> str | None:
    """Return an exported value first, then a dotenv value, without mutation."""
    return os.environ.get(name, values.get(name))


def _env_bool(name: str, default: bool, values: Mapping[str, str | None]) -> bool:
    raw = _value(name, values)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(
    name: str,
    default: int,
    values: Mapping[str, str | None],
    *,
    minimum: int | None = None,
) -> int:
    raw = _value(name, values)
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

    @property
    def configured(self) -> bool:
        if self.webhook_url:
            return True
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_dotenv_files() -> dict[str, str | None]:
    """Parse local config without copying credentials into ``os.environ``."""
    values: dict[str, str | None] = {}
    # Keep the historical precedence: exported env, config/.env, then root .env.
    if ENV_FILE.is_file():
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError:
            pass
        for key, value in dotenv_values(ENV_FILE).items():
            values.setdefault(key, value)
    root_env = REPO_ROOT / ".env"
    if root_env.is_file():
        for key, value in dotenv_values(root_env).items():
            values.setdefault(key, value)
    return values


def get_transfer_settings() -> TransferSettings:
    values = load_dotenv_files()
    strategy = (_value("AUTO_TRANSFER_FEE_STRATEGY", values) or "normal").strip().lower()
    strategy = strategy or "normal"
    settings = TransferSettings(
        enabled=_env_bool("AUTO_TRANSFER_ENABLED", False, values),
        dry_run=_env_bool("AUTO_TRANSFER_DRY_RUN", True, values),
        dest_addr=(_value("AUTO_TRANSFER_DEST_ADDR", values) or "").strip(),
        live_confirm=(_value("AUTO_TRANSFER_LIVE_CONFIRM", values) or "").strip(),
        min_balance_sats=_env_int(
            "AUTO_TRANSFER_MIN_BALANCE_SATS", 5000, values, minimum=0
        ),
        min_send_sats=_env_int("AUTO_TRANSFER_MIN_SEND_SATS", 546, values, minimum=0),
        default_fee_rate=_env_int(
            "AUTO_TRANSFER_DEFAULT_FEE_RATE", 15, values, minimum=1
        ),
        max_fee_rate=_env_int("AUTO_TRANSFER_MAX_FEE_RATE", 250, values, minimum=1),
        fee_strategy=strategy,
        fee_target_blocks=_env_int(
            "AUTO_TRANSFER_FEE_TARGET_BLOCKS", 2, values, minimum=1
        ),
        rbf=_env_bool("AUTO_TRANSFER_RBF", True, values),
        confirmed_only=_env_bool("AUTO_TRANSFER_CONFIRMED_ONLY", True, values),
        max_fee_sats=_env_int("AUTO_TRANSFER_MAX_FEE_SATS", 100_000, values, minimum=1),
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
    values = load_dotenv_files()
    return NotifySettings(
        enabled=_env_bool("NOTIFY_ENABLED", False, values),
        webhook_url=(_value("NOTIFY_WEBHOOK_URL", values) or "").strip(),
        telegram_bot_token=(
            _value("NOTIFY_TELEGRAM_BOT_TOKEN", values) or ""
        ).strip(),
        telegram_chat_id=(_value("NOTIFY_TELEGRAM_CHAT_ID", values) or "").strip(),
    )


def validate_notify_settings(settings: NotifySettings) -> list[str]:
    errors: list[str] = []
    if not settings.enabled:
        return errors
    if not settings.configured:
        errors.append(
            "NOTIFY_ENABLED but neither NOTIFY_WEBHOOK_URL nor "
            "NOTIFY_TELEGRAM_BOT_TOKEN+NOTIFY_TELEGRAM_CHAT_ID is set"
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
    ch = ",".join(channels) if channels else "(none)"
    return f"enabled={cfg.enabled} channels={ch}"


def ensure_config_dir() -> Path:
    from btc_puzzle_lab.paths import CONFIG_DIR

    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    return CONFIG_DIR
