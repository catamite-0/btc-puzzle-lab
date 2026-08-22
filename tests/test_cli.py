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
        a.dest
        for a in build_parser(hide_advanced=False)._subparsers._group_actions[0]._choices_actions
    }
    assert hidden <= everything


def test_hub_setup_is_reachable_without_help_all():
    """`hub` raises FileNotFoundError pointing at relay-keygen when the keypair is
    missing, so hiding relay-keygen behind a flag hides a prerequisite."""
    from btc_puzzle_lab.cli import build_parser

    listed = {a.dest for a in build_parser()._subparsers._group_actions[0]._choices_actions}
    assert {"hub", "relay-keygen"} <= listed


def test_hidden_commands_still_parse():
    from btc_puzzle_lab.cli import _ADVANCED, build_parser

    parser = build_parser()
    needs_arg = {
        "verify": ["1"],
        "coverage": ["1"],
        "run": ["1"],
        "unseal": ["x"],
        "verify-dry-run": ["p"],
        "strategy": ["1"],
    }
    for names in _ADVANCED.values():
        for name in names:
            args = parser.parse_args([name, *needs_arg.get(name, [])])
            assert callable(args.func), name


def test_host_is_an_alias_of_adapt():
    from btc_puzzle_lab.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["host"]).func is parser.parse_args(["adapt"]).func


def test_auto_plan_only_is_an_alias_of_plan():
    from btc_puzzle_lab.cli import build_parser

    parser = build_parser()
    canonical = parser.parse_args(["auto", "71", "--plan"])
    legacy = parser.parse_args(["auto", "71", "--plan-only"])
    catalog_canonical = parser.parse_args(["auto", "--plan"])
    catalog_legacy = parser.parse_args(["auto", "--plan-only"])

    assert canonical.plan_only is True
    assert legacy.plan_only is True
    assert catalog_canonical.plan_only is True
    assert catalog_legacy.plan_only is True
    assert catalog_canonical.puzzle is None
    assert catalog_legacy.puzzle is None
    assert canonical.func is legacy.func
    assert catalog_canonical.func is catalog_legacy.func


def test_every_other_auto_option_is_explicitly_rejected_by_read_only_plan():
    from btc_puzzle_lab.cli import _auto_plan_conflicts, build_parser

    parser = build_parser()
    auto_parser = parser._subparsers._group_actions[0].choices["auto"]
    checked: set[str] = set()
    for action in auto_parser._actions:
        if action.dest in {"help", "puzzle", "plan_only"}:
            continue
        option = action.option_strings[0]
        for prefix in (["auto", "--plan"], ["auto", "71", "--plan"]):
            argv = [*prefix, option]
            if action.nargs != 0:
                if action.choices:
                    argv.append(str(next(iter(action.choices))))
                elif action.type in {int, float}:
                    argv.append("1")
                else:
                    argv.append("test-value")
            parsed = parser.parse_args(argv)
            conflicts = _auto_plan_conflicts(parsed)

            assert option in conflicts, f"{option} would be silently ignored by {' '.join(prefix)}"
        checked.add(option)

    assert {"--dest", "--live", "--engine", "--dp", "--no-build"} <= checked


def test_auto_help_explains_optional_id_and_the_two_read_only_scopes():
    from btc_puzzle_lab.cli import build_parser

    parser = build_parser()
    auto_parser = parser._subparsers._group_actions[0].choices["auto"]
    help_text = auto_parser.format_help()
    normalized = " ".join(help_text.split())

    assert "[puzzle]" in help_text
    assert "required unless --plan is used" in normalized
    assert "without an id rank the package catalog" in normalized
    assert "with an id" in normalized
