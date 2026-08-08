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

    @property
    def live_ok(self) -> bool:
        return self.live_confirm == LIVE_CONFIRM_PHRASE


def load_dotenv_files() -> None:
    # Optional local config; never required for search/audit-only use.
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


def ensure_config_dir() -> Path:
    from btc_puzzle_lab.paths import CONFIG_DIR

    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    return CONFIG_DIR
