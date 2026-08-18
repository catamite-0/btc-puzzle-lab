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


def test_help_leads_with_auto_and_hides_the_layers_below_it():
    """A flat list of 25 commands buried the one anybody starts from."""
    from btc_puzzle_lab.cli import _ADVANCED, build_parser

    listed = {a.dest for a in build_parser()._subparsers._group_actions[0]._choices_actions}
    assert "auto" in listed
    hidden = {name for names in _ADVANCED.values() for name in names}
    assert not (listed & hidden), f"advanced commands still in the short help: {listed & hidden}"

    everything = {
        a.dest for a in build_parser(hide_advanced=False)._subparsers._group_actions[0]._choices_actions
    }
    assert hidden <= everything


def test_hidden_commands_still_parse():
    from btc_puzzle_lab.cli import _ADVANCED, build_parser

    parser = build_parser()
    needs_arg = {"verify": ["1"], "coverage": ["1"], "run": ["1"], "unseal": ["x"],
                 "verify-dry-run": ["p"], "strategy": ["1"]}
    for names in _ADVANCED.values():
        for name in names:
            args = parser.parse_args([name, *needs_arg.get(name, [])])
            assert callable(args.func), name


def test_host_is_an_alias_of_adapt():
    from btc_puzzle_lab.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["host"]).func is parser.parse_args(["adapt"]).func
