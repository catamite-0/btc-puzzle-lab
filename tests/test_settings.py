import pytest

from btc_puzzle_lab.paths import clear_path_cache
from btc_puzzle_lab.settings import (
    LIVE_CONFIRM_PHRASE,
    get_transfer_settings,
    validate_transfer_settings,
)


def _isolate_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    for key in (
        "AUTO_TRANSFER_ENABLED",
        "AUTO_TRANSFER_DRY_RUN",
        "AUTO_TRANSFER_DEST_ADDR",
        "AUTO_TRANSFER_LIVE_CONFIRM",
        "AUTO_TRANSFER_DEFAULT_FEE_RATE",
        "AUTO_TRANSFER_MAX_FEE_RATE",
        "AUTO_TRANSFER_FEE_STRATEGY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_defaults_are_safe(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    settings = get_transfer_settings()
    assert settings.enabled is False
    assert settings.dry_run is True
    assert validate_transfer_settings(settings) == []


def test_live_requires_confirm_phrase(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_TRANSFER_ENABLED", "true")
    monkeypatch.setenv("AUTO_TRANSFER_DRY_RUN", "false")
    monkeypatch.setenv("AUTO_TRANSFER_DEST_ADDR", "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH")
    monkeypatch.setenv("AUTO_TRANSFER_LIVE_CONFIRM", "nope")
    settings = get_transfer_settings()
    errors = validate_transfer_settings(settings)
    assert any(LIVE_CONFIRM_PHRASE in e for e in errors)


def test_live_ok_with_confirm(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_TRANSFER_ENABLED", "true")
    monkeypatch.setenv("AUTO_TRANSFER_DRY_RUN", "false")
    monkeypatch.setenv("AUTO_TRANSFER_DEST_ADDR", "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH")
    monkeypatch.setenv("AUTO_TRANSFER_LIVE_CONFIRM", LIVE_CONFIRM_PHRASE)
    settings = get_transfer_settings()
    assert validate_transfer_settings(settings) == []


def test_fee_rate_bounds(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_TRANSFER_DEFAULT_FEE_RATE", "300")
    monkeypatch.setenv("AUTO_TRANSFER_MAX_FEE_RATE", "250")
    with pytest.raises(ValueError, match="DEFAULT_FEE_RATE"):
        get_transfer_settings()
