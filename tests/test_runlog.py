from btc_puzzle_lab.runlog import _sanitize, log_event, read_events


def test_sanitize_strips_nested_secrets():
    clean = _sanitize(
        {
            "puzzle_id": 1,
            "private_key_hex": "deadbeef",
            "nested": {"tx_hex": "010203", "ok": True},
            "wif": "secret",
        }
    )
    assert clean == {"puzzle_id": 1, "nested": {"ok": True}}
    assert "private_key_hex" not in clean
    assert "wif" not in clean


def test_log_event_redacts(tmp_path):
    path = tmp_path / "runs.jsonl"
    log_event("hit", log_path=path, private_key_hex="aa", puzzle_id=1)
    rows = read_events(path)
    assert rows[0]["event"] == "hit"
    assert rows[0]["puzzle_id"] == 1
    assert "private_key_hex" not in rows[0]


def test_sanitize_walks_into_lists():
    # Only dicts were recursed into, so a list of rows carried secrets straight
    # through into state/runs.jsonl.
    clean = _sanitize(
        {
            "puzzle_id": 1,
            "hits": [
                {"address": "1abc", "private_key_hex": "deadbeef"},
                {"address": "1def", "wif": "secret"},
            ],
            "pairs": [[{"tx_hex": "0102"}]],
        }
    )
    assert clean == {
        "puzzle_id": 1,
        "hits": [{"address": "1abc"}, {"address": "1def"}],
        "pairs": [[{}]],
    }


def test_log_event_redacts_inside_lists(tmp_path):
    path = tmp_path / "runs.jsonl"
    log_event("batch", log_path=path, results=[{"puzzle_id": 3, "private_key_hex": "aa"}])
    text = path.read_text(encoding="utf-8")
    assert "aa" not in text.replace('"batch"', "")
    assert read_events(path)[0]["results"] == [{"puzzle_id": 3}]
