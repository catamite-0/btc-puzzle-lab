import threading

from btc_puzzle_lab.hits import Hit, append_hit, read_hits
from btc_puzzle_lab.paths import clear_path_cache


def _hit() -> Hit:
    return Hit(
        puzzle_id=1,
        address="1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH",
        private_key_hex="1",
        engine="sequential",
        found_at="2026-08-09T00:00:00+00:00",
        verified=True,
    )


def test_append_hit_dedupes_under_concurrent_writers(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    clear_path_cache()
    results: list[bool] = []

    def writer() -> None:
        results.append(append_hit(_hit()).appended)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert results.count(False) == 7
    assert len(read_hits()) == 1
    assert (tmp_path / "state" / "HITS.jsonl.lock").is_file()
