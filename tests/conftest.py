import pytest

from btc_puzzle_lab.paths import clear_path_cache

_ENGINE_PATH_VARS = (
    "KEYHUNT_PATH",
    "KANGAROO_PATH",
    "BITCRACK_PATH",
    "RCKANGAROO_PATH",
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
    clear_path_cache()
    yield
    clear_path_cache()
