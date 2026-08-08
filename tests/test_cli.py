from btc_puzzle_lab.cli import main


def test_cli_list_ok():
    assert main(["list"]) == 0


def test_cli_verify_known_puzzle():
    assert main(["verify", "1"]) == 0


def test_cli_unknown_puzzle_clean_error(capsys):
    code = main(["verify", "999"])
    err = capsys.readouterr().err
    assert code == 2
    assert "unknown puzzle #999" in err
    assert "Traceback" not in err


def test_cli_strategy_and_engines():
    assert main(["strategy", "20"]) == 0
    assert main(["engines"]) == 0


def test_cli_run_tiny_puzzle(tmp_path, monkeypatch):
    monkeypatch.setenv("BTC_PUZZLE_LAB_HOME", str(tmp_path))
    from btc_puzzle_lab.paths import clear_path_cache

    clear_path_cache()
    assert main(["run", "1", "--engine", "sequential", "--no-progress"]) == 0
    assert (tmp_path / "state" / "HITS.jsonl").is_file()
