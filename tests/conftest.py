import os

import pytest

from btc_puzzle_lab.paths import clear_path_cache

_ENGINE_PATH_VARS = (
    "KEYHUNT_PATH",
    "KANGAROO_PATH",
    "BITCRACK_PATH",
    "RCKANGAROO_PATH",
)

# Keys `bootstrap_config` writes straight into os.environ so the running process
# sees them without a reload. They must not survive the test that wrote them: a
# leaked AUTO_TRANSFER_ENABLED=true turns a later test's expected "skipped" sweep
# into a real explorer call.
_CONFIG_VARS = (
    "AUTO_TRANSFER_ENABLED",
    "AUTO_TRANSFER_DRY_RUN",
    "AUTO_TRANSFER_DEST_ADDR",
    "AUTO_TRANSFER_LIVE_CONFIRM",
    "NOTIFY_ENABLED",
    "NOTIFY_WEBHOOK_URL",
    "NOTIFY_TELEGRAM_BOT_TOKEN",
    "NOTIFY_TELEGRAM_CHAT_ID",
    "RELAY_URL",
    "RELAY_SEAL_PUBKEY",
    "RELAY_SEAL_SECRET",
    "RELAY_TOKEN",
)


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """Point every test at a throwaway workspace.

    Two real problems this prevents:

    - Anything that logs an event or records a hit used to write into the
      developer's own ``state/``. A pytest run left fake ``transfer_broadcast``
      rows in the live audit trail, which is exactly the evidence you would be
      reading if a real sweep ever went wrong.
    - ``load_engine_env()`` calls ``load_dotenv``, which writes ``*_PATH`` into
      ``os.environ`` permanently. Those leaked between tests and pointed the
      engine resolver at real solver binaries, so one test silently executed a
      live search until its timeout expired.

    Tests that need a specific workspace still override this by setting
    ``BTC_PUZZLE_LAB_HOME`` themselves.
    """
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    for var in _ENGINE_PATH_VARS:
        monkeypatch.delenv(var, raising=False)
    saved = {var: os.environ.get(var) for var in _CONFIG_VARS}
    clear_path_cache()
    yield
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    clear_path_cache()
