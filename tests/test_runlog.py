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
