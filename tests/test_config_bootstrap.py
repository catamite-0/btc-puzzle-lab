import stat

import pytest

from btc_puzzle_lab.settings import (
    LIVE_CONFIRM_PHRASE,
    bootstrap_config,
    get_notify_settings,
    get_transfer_settings,
    validate_transfer_settings,
    write_env_values,
)

DEST = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"

# bootstrap_config writes into os.environ on purpose so the running process sees
# the new settings; conftest's isolated_workspace fixture puts those keys back
# after every test, so nothing here leaks into the next one.


def test_dest_enables_transfer_but_stays_dry_run():
    update = bootstrap_config(dest_addr=DEST)
    assert update.dest_addr == DEST
    assert not update.live

    settings = get_transfer_settings()
    assert settings.enabled
    assert settings.dry_run
    # The confirm phrase must not be written by a plain --dest.
    assert not settings.live_ok
    assert validate_transfer_settings(settings) == []


def test_live_writes_the_confirm_phrase_and_clears_dry_run():
    bootstrap_config(dest_addr=DEST, live=True)
    settings = get_transfer_settings()
    assert settings.enabled and not settings.dry_run
    assert settings.live_confirm == LIVE_CONFIRM_PHRASE
    assert settings.live_ok
    assert validate_transfer_settings(settings) == []


def test_live_without_a_destination_is_refused():
    with pytest.raises(ValueError, match="--dest"):
        bootstrap_config(live=True)


def test_invalid_destination_is_refused():
    with pytest.raises(ValueError, match="not a valid BTC address"):
        bootstrap_config(dest_addr="not-an-address")


def test_notify_url_must_be_http():
    with pytest.raises(ValueError, match="http"):
        bootstrap_config(notify_url="ftp://example.com/hook")


def test_notify_enables_the_webhook_channel():
    update = bootstrap_config(notify_url="https://ntfy.sh/topic")
    assert update.notify_channels == ("webhook",)
    notify = get_notify_settings()
    assert notify.enabled and notify.configured


def test_telegram_needs_both_halves():
    with pytest.raises(ValueError, match="both"):
        bootstrap_config(telegram_token="abc")


def test_nothing_to_write_leaves_the_file_alone(tmp_path):
    update = bootstrap_config()
    assert update.keys == ()
    assert "unchanged" in update.format()


def test_env_file_is_owner_only_and_preserves_hand_edits():
    from btc_puzzle_lab.paths import ENV_FILE

    write_env_values({"AUTO_TRANSFER_MAX_FEE_SATS": "12345"})
    bootstrap_config(dest_addr=DEST, notify_url="https://ntfy.sh/topic")

    path = ENV_FILE
    text = path.read_text(encoding="utf-8")
    assert "AUTO_TRANSFER_MAX_FEE_SATS=12345" in text
    assert f"AUTO_TRANSFER_DEST_ADDR={DEST}" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rewriting_a_key_does_not_duplicate_it():
    from btc_puzzle_lab.paths import ENV_FILE

    bootstrap_config(dest_addr=DEST)
    bootstrap_config(dest_addr="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    dest_lines = [line for line in lines if line.startswith("AUTO_TRANSFER_DEST_ADDR=")]
    assert len(dest_lines) == 1
    assert dest_lines[0].endswith("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")


def test_relay_requires_a_token():
    from btc_puzzle_lab.relay import generate_relay_keypair

    _, pub = generate_relay_keypair()
    with pytest.raises(ValueError, match="relay-token"):
        bootstrap_config(
            relay_url="https://control.example:8787/hit",
            relay_seal_pubkey=pub,
        )


def test_dest_and_relay_cannot_both_be_set():
    from btc_puzzle_lab.relay import generate_relay_keypair

    _, pub = generate_relay_keypair()
    with pytest.raises(ValueError, match="cannot both be set"):
        bootstrap_config(
            dest_addr=DEST,
            relay_url="https://control.example:8787/hit",
            relay_seal_pubkey=pub,
            relay_token="control-hub-token-1",
        )


def test_relay_is_refused_when_dest_already_in_env():
    from btc_puzzle_lab.relay import generate_relay_keypair

    bootstrap_config(dest_addr=DEST)
    _, pub = generate_relay_keypair()
    with pytest.raises(ValueError, match="cannot both be set"):
        bootstrap_config(
            relay_url="https://control.example:8787/hit",
            relay_seal_pubkey=pub,
            relay_token="control-hub-token-1",
        )
