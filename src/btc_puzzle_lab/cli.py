from __future__ import annotations

import argparse
import sys
from pathlib import Path

from btc_puzzle_lab import __version__
from btc_puzzle_lab.audit import audit_hits, export_audit_report
from btc_puzzle_lab.autorun import STAGES, Stage, run_auto
from btc_puzzle_lab.batch import (
    batch_plan_path,
    build_plan,
    format_plan,
    format_status,
    load_plan,
    run_batch,
    save_plan,
)
from btc_puzzle_lab.catalog import get_puzzle, load_puzzles
from btc_puzzle_lab.catalog_import import DEFAULT_EXPORT_URL, import_catalog
from btc_puzzle_lab.coverage import format_coverage, load_coverage
from btc_puzzle_lab.crypto import privkey_bytes, privkey_to_p2pkh_address
from btc_puzzle_lab.doctor import doctor_ok, format_doctor, run_doctor
from btc_puzzle_lab.engines import format_engine_status
from btc_puzzle_lab.hits import read_hits
from btc_puzzle_lab.hub import serve_hub
from btc_puzzle_lab.loop import format_loop_result, format_watch_result, run_once, run_watch
from btc_puzzle_lab.paths import HITS_FILE, STATE_DIR, coverage_path
from btc_puzzle_lab.relay import (
    flush_outbox,
    generate_relay_keypair,
    generate_relay_token,
    unseal_hit,
    write_relay_secret,
)
from btc_puzzle_lab.search import DEFAULT_CHUNK_SIZE, run_puzzle
from btc_puzzle_lab.settings import (
    bootstrap_config,
    get_transfer_settings,
    validate_transfer_settings,
)
from btc_puzzle_lab.strategy import (
    adapt_recommendations,
    format_host_profile,
    plan_strategy,
    probe_host,
)
from btc_puzzle_lab.summary import build_summary, format_summary
from btc_puzzle_lab.toolchain import (
    INSTALLABLE,
    SELFCHECK_PUZZLES,
    format_install_results,
    format_selfcheck_results,
    install_engines,
    selfcheck_engines,
)
from btc_puzzle_lab.transfer import (
    TransferResult,
    broadcast_dry_run_file,
    format_transfer_policy,
    sweep_hit,
    verify_dry_run_file,
)

_ENGINE_CHOICES = [
    "sequential",
    "window",
    "inject-known",
    "keyhunt",
    "bitcrack",
    "kangaroo",
    "rckangaroo",
]


def _print_transfer(result: TransferResult) -> None:
    print(f"transfer[{result.status}]: {result.message}")
    if result.dest_addr:
        print(f"  dest={result.dest_addr}")
    if result.send_amount is not None:
        print(
            f"  send_amount={result.send_amount} sats fee={result.fee} "
            f"fee_rate={result.fee_rate} vsize={result.vsize}"
        )
    if result.input_count is not None:
        print(f"  inputs={result.input_count} rbf={result.rbf}")
    if result.tx_fingerprint:
        print(f"  tx_fingerprint={result.tx_fingerprint}")
    if result.dry_run_path:
        print(f"  dry_run_path={result.dry_run_path}")
    if result.txid:
        print(f"  txid={result.txid}")
    if result.chain_status:
        print(f"  chain_status={result.chain_status}")


def cmd_config(args: argparse.Namespace) -> int:
    token = args.relay_token
    generated: str | None = None
    if args.new_relay_token:
        if token:
            print("error: pass either --relay-token or --new-relay-token", file=sys.stderr)
            return 2
        generated = generate_relay_token()
        token = generated
    try:
        update = bootstrap_config(
            dest_addr=args.dest,
            notify_url=args.notify,
            telegram_token=args.telegram_token,
            telegram_chat=args.telegram_chat,
            relay_url=args.relay,
            relay_seal_pubkey=args.relay_seal_pubkey,
            relay_token=token,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(update.format())
    if generated:
        print(f"relay token : {generated}")
        print("copy onto hunt boxes: btc-puzzle-lab auto 140 --relay-token <that-token>")
    return 0


def cmd_relay_keygen(_: argparse.Namespace) -> int:
    secret, pubkey = generate_relay_keypair()
    path = write_relay_secret(secret)
    print(f"wrote secret : {path} (mode 0600; keep this control host only)")
    print(f"pubkey       : {pubkey}")
    print("on this control VPS:")
    print("  btc-puzzle-lab config --dest <btc-address> --notify https://...")
    print("  btc-puzzle-lab config --new-relay-token")
    print("  btc-puzzle-lab hub --host 0.0.0.0 --port 8787")
    print("on each hunt VPS (no relay-secret, dest stays on the hub):")
    print(
        "  btc-puzzle-lab auto 140 --relay https://<control>:8787/hit "
        f"--relay-seal-pubkey {pubkey} --relay-token <same-token>"
    )
    return 0


def cmd_unseal(args: argparse.Namespace) -> int:
    raw = args.token
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    if not raw:
        print("error: pass a bpl1. token or --file", file=sys.stderr)
        return 2
    try:
        hit = unseal_hit(raw)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"puzzle  : #{hit.puzzle_id}")
    print(f"address : {hit.address}")
    if hit.engine:
        print(f"engine  : {hit.engine}")
    if args.show_key:
        print(f"key     : {hit.private_key_hex}")
    else:
        print("key     : (pass --show-key to print)")
    return 0


def cmd_relay_flush(_: argparse.Namespace) -> int:
    results = flush_outbox()
    if not results:
        print("relay outbox: nothing pending")
        return 0
    fails = 0
    for item in results:
        mark = "ok" if item.ok else "fail"
        print(f"relay[{mark}]: {item.message}")
        if not item.ok:
            fails += 1
    return 1 if fails else 0


def cmd_hub(args: argparse.Namespace) -> int:
    try:
        serve_hub(
            host=args.host,
            port=args.port,
            sweep=not args.no_sweep,
            notify=not args.no_notify,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("hub stopped")
        return 0
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    print(f"{'ID':>4}  {'bits':>4}  {'engine':<12}  address")
    for puzzle in load_puzzles():
        print(
            f"{puzzle.id:>4}  {puzzle.bits:>4}  {puzzle.engine_default:<12}  {puzzle.address}"
        )
        if puzzle.notes:
            print(f"      {puzzle.notes}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    puzzle = get_puzzle(args.puzzle)
    if puzzle.practice_solution is None:
        print(f"puzzle #{puzzle.id} has no practice solution in catalog", file=sys.stderr)
        return 1
    pk = privkey_bytes(puzzle.practice_solution)
    derived = privkey_to_p2pkh_address(pk)
    ok = derived == puzzle.address
    print(f"puzzle #{puzzle.id}")
    print(f"  catalog address : {puzzle.address}")
    print(f"  derived address : {derived}")
    print(f"  match           : {'yes' if ok else 'NO'}")
    return 0 if ok else 2


def cmd_run(args: argparse.Namespace) -> int:
    puzzle = get_puzzle(args.puzzle)
    run_kwargs = dict(
        engine=args.engine,
        window=args.window,
        threads=args.threads,
        workers=args.workers,
        resume=args.resume,
        progress=not args.no_progress,
        coverage=args.coverage,
        chunk_size=args.chunk_size,
        order=args.order,
        seed=args.seed,
        max_chunks=args.max_chunks,
        dp=16 if args.dp is None else args.dp,
        timeout=_loop_timeout(args),
    )
    if args.auto:
        plan = plan_strategy(puzzle)
        print(f"auto: {plan.format()}")
        run_kwargs.update(
            engine=args.engine or plan.engine,
            window=plan.window,
            threads=plan.threads,
            workers=plan.workers,
            coverage=plan.coverage,
            chunk_size=plan.chunk_size,
            order=plan.order,
            seed=args.seed if args.seed is not None else plan.seed,
            max_chunks=args.max_chunks if args.max_chunks is not None else plan.max_chunks,
            dp=args.dp if args.dp is not None else plan.dp,
        )
    outcome = run_puzzle(puzzle, **run_kwargs)
    print(f"engine={outcome.engine}: {outcome.message}")
    if outcome.coverage is not None:
        print(format_coverage(outcome.coverage))
        if outcome.chunks_scanned:
            print(f"chunks_scanned={outcome.chunks_scanned}")
    if outcome.hit is None:
        # Coverage exhaustion / partial miss is an informative non-hit, not a crash.
        if outcome.coverage is not None and "coverage complete" in outcome.message:
            return 0
        return 1
    print(f"hit puzzle #{outcome.hit.puzzle_id} address={outcome.hit.address}")
    print(f"recorded in {HITS_FILE}")
    if args.show_key:
        print(f"private_key_hex={outcome.hit.private_key_hex}")
    if args.transfer:
        settings = get_transfer_settings()
        errors = validate_transfer_settings(settings)
        if errors:
            for err in errors:
                print(f"config error: {err}", file=sys.stderr)
            return 2
        result = sweep_hit(outcome.hit, settings=settings)
        _print_transfer(result)
        if result.status in {"dry_run", "broadcast"}:
            return 0
        if result.status == "skipped":
            return 3
        return 1
    return 0


def cmd_strategy(args: argparse.Namespace) -> int:
    puzzle = get_puzzle(args.puzzle)
    plan = plan_strategy(puzzle)
    print(f"puzzle #{puzzle.id} bits={puzzle.bits}")
    print(plan.format())
    return 0


def cmd_engines(args: argparse.Namespace) -> int:
    action = getattr(args, "engines_action", "status") or "status"
    if action == "install":
        only = None
        if args.only:
            only = [part.strip() for part in args.only.split(",") if part.strip()]
        try:
            results = install_engines(only, force=args.force)
        except (RuntimeError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(format_install_results(results))
        print()
        print(format_engine_status())
        hard_fail = [r for r in results if not r.ok and r.name in INSTALLABLE]
        if args.no_selfcheck:
            print()
            print("self-check skipped (--no-selfcheck); engines are unverified")
            return 1 if hard_fail else 0
        installed = [r.name for r in results if r.ok and r.name in SELFCHECK_PUZZLES]
        if installed:
            print()
            checks = selfcheck_engines(installed, timeout=args.selfcheck_timeout)
            print(format_selfcheck_results(checks))
            if any(not c.ok for c in checks):
                return 1
        return 1 if hard_fail else 0
    if action == "selfcheck":
        only = None
        if args.only:
            only = [part.strip() for part in args.only.split(",") if part.strip()]
        checks = selfcheck_engines(only, timeout=args.selfcheck_timeout)
        print(format_selfcheck_results(checks))
        return 1 if any(not c.ok for c in checks) else 0
    print(format_engine_status())
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    checks = run_doctor()
    print(format_doctor(checks))
    return 0 if doctor_ok(checks) else 1


def cmd_coverage(args: argparse.Namespace) -> int:
    if args.puzzle is not None:
        ledger = load_coverage(args.puzzle)
        if ledger is None:
            print(f"no coverage ledger at {coverage_path(args.puzzle)}")
            return 1
        print(format_coverage(ledger))
        return 0
    found = sorted(STATE_DIR.glob("coverage_*.json")) if STATE_DIR.exists() else []
    if not found:
        print(f"no coverage ledgers in {STATE_DIR}")
        return 1
    for path in found:
        try:
            puzzle_id = int(path.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        ledger = load_coverage(puzzle_id)
        if ledger is not None:
            print(format_coverage(ledger))
            print()
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    hits = read_hits()
    if not hits:
        print(f"no hits in {HITS_FILE}")
        return 1
    results = audit_hits(check_balance=args.balance)
    failures = 0
    for result in results:
        status = "OK" if result.address_ok and not result.error else "FAIL"
        if status != "OK":
            failures += 1
        balance = (
            f"{result.balance_sats} sats"
            if result.balance_sats is not None
            else "n/a"
        )
        print(
            f"[{status}] puzzle #{result.hit.puzzle_id} "
            f"address={result.hit.address} balance={balance} engine={result.hit.engine}"
        )
        if result.error:
            print(f"         error: {result.error}")
    if args.export:
        export_path = Path(args.export)
        export_audit_report(results, export_path)
        print(f"exported audit report to {export_path}")
    return 1 if failures else 0


def cmd_transfer(args: argparse.Namespace) -> int:
    settings = get_transfer_settings()
    errors = validate_transfer_settings(settings)
    if errors:
        for err in errors:
            print(f"config error: {err}", file=sys.stderr)
        return 2
    print(f"policy: {format_transfer_policy(settings)}")
    if args.broadcast_dry_run:
        result = broadcast_dry_run_file(args.broadcast_dry_run, settings=settings)
        _print_transfer(result)
        if result.status == "broadcast":
            return 0
        if result.status == "skipped":
            return 3
        return 1
    hits = read_hits()
    if not hits:
        print(f"no hits in {HITS_FILE}")
        return 1
    if args.puzzle is not None:
        selected = [h for h in hits if h.puzzle_id == args.puzzle]
        if not selected:
            print(f"no hits for puzzle #{args.puzzle}")
            return 1
        hit = selected[-1]
    else:
        hit = hits[-1]
    print(f"sweeping puzzle #{hit.puzzle_id} address={hit.address}")
    confirmed_only = None if not args.allow_unconfirmed else False
    result = sweep_hit(
        hit,
        settings=settings,
        fee_rate=args.fee_rate,
        confirmed_only=confirmed_only,
    )
    _print_transfer(result)
    if result.status == "dry_run" and result.dry_run_path and args.verify_dry_run:
        verify = verify_dry_run_file(
            result.dry_run_path,
            expected_dest=settings.dest_addr or None,
            min_send_sats=settings.min_send_sats,
        )
        print(
            f"dry-run verify: {'OK' if verify.ok else 'FAIL'} — {verify.message} "
            f"(inputs={verify.input_count} outputs={verify.output_count} "
            f"vsize={verify.vsize} dest={verify.dest_addr} send={verify.send_amount})"
        )
        if not verify.ok:
            return 1
    if result.status in {"dry_run", "broadcast"}:
        return 0
    if result.status == "skipped":
        # Intentional gate (disabled / dry-run policy) — not a crash, but not success.
        return 3
    return 1


def cmd_verify_dry_run(args: argparse.Namespace) -> int:
    settings = get_transfer_settings()
    result = verify_dry_run_file(
        args.path,
        expected_dest=(settings.dest_addr or None) if args.check_dest else None,
        min_send_sats=settings.min_send_sats if args.check_dest else None,
    )
    print(f"[{'OK' if result.ok else 'FAIL'}] {result.message}")
    print(f"  path={result.path}")
    if result.fingerprint:
        print(f"  fingerprint={result.fingerprint}")
    if result.dest_addr:
        print(f"  dest={result.dest_addr} send_amount={result.send_amount}")
    if result.version is not None:
        print(
            f"  version={result.version} inputs={result.input_count} "
            f"outputs={result.output_count} size_bytes={result.size_bytes} "
            f"vsize={result.vsize}"
        )
    return 0 if result.ok else 1


def cmd_summary(args: argparse.Namespace) -> int:
    summary = build_summary(recent=args.recent)
    print(format_summary(summary))
    return 0


def cmd_import_catalog(args: argparse.Namespace) -> int:
    csv_path = Path(args.from_csv) if args.from_csv else None
    output = Path(args.output) if args.output else None
    url = args.url
    if csv_path is not None:
        url = None
    result = import_catalog(
        url=url,
        csv_path=csv_path,
        output=output,
        include_solutions=not args.no_solutions,
    )
    print(f"wrote {result.path}")
    print(
        f"puzzles={result.count} solved={result.solved} unsolved={result.unsolved} "
        f"with_pubkey={result.with_pubkey} with_solution={result.with_solution}"
    )
    print(f"source={result.source}")
    print("tip: run `btc-puzzle-lab list` to confirm the active catalog")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    ids = [int(part) for part in args.ids.split(",")] if args.ids else None
    plan = build_plan(
        status=args.status,
        bits_min=args.bits_min,
        bits_max=args.bits_max,
        puzzle_ids=ids,
    )
    path = Path(args.output) if args.output else batch_plan_path()
    save_plan(plan, path)
    print(f"wrote {path}")
    print(format_plan(plan, verbose=args.verbose))
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    path = Path(args.plan) if args.plan else batch_plan_path()
    plan = load_plan(path)
    if plan is None:
        print(f"error: no batch plan at {path} (run: btc-puzzle-lab plan)", file=sys.stderr)
        return 2
    result = run_batch(
        plan,
        limit=args.limit,
        resume=not args.no_resume,
        stop_on_hit=args.stop_on_hit,
        include_blocked=args.include_blocked,
        progress=not args.no_progress,
        plan_path=path,
    )
    print(
        f"batch attempted={result.attempted} hits={result.hits} done={result.done} "
        f"errors={result.errors} skipped={result.skipped} "
        f"stopped_early={result.stopped_early}"
    )
    print(f"plan={result.plan_path}")
    if result.errors:
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.plan) if args.plan else None
    plan = load_plan(path) if path else load_plan()
    if plan is None:
        target = path if path is not None else batch_plan_path()
        print(
            f"error: no batch plan at {target} (run: btc-puzzle-lab plan)",
            file=sys.stderr,
        )
        return 1
    print(format_status(plan))
    return 0


def cmd_host(_: argparse.Namespace) -> int:
    profile = probe_host()
    print(format_host_profile(profile))
    return 0


def cmd_adapt(_: argparse.Namespace) -> int:
    profile = probe_host()
    print(format_host_profile(profile))
    print()
    print("recommendations:")
    for tip in adapt_recommendations(profile):
        print(f"  - {tip}")
    return 0


def _loop_ids(args: argparse.Namespace) -> list[int] | None:
    return [int(part) for part in args.ids.split(",")] if args.ids else None


def _loop_timeout(args: argparse.Namespace) -> float | None:
    if getattr(args, "max_seconds", None) is not None:
        return float(args.max_seconds)
    return None


def _loop_plan_path(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "plan_file", None)
    return Path(raw) if raw else None


def cmd_once(args: argparse.Namespace) -> int:
    result = run_once(
        sync=not args.no_sync,
        status=args.status,
        bits_min=args.bits_min,
        bits_max=args.bits_max,
        puzzle_ids=_loop_ids(args),
        limit=args.limit,
        stop_on_hit=not args.no_stop_on_hit,
        resource=args.resource,
        require_doctor=not args.no_doctor,
        audit=not args.no_audit,
        check_balance=args.balance,
        transfer=not args.no_transfer,
        notify=not args.no_notify,
        progress=not args.no_progress,
        timeout=_loop_timeout(args),
        plan_path=_loop_plan_path(args),
    )
    print(format_loop_result(result))
    for item in result.transfers:
        _print_transfer(item)
    return 0 if result.ok else 1


def cmd_watch(args: argparse.Namespace) -> int:
    result = run_watch(
        max_hours=args.max_hours,
        max_passes=args.max_passes,
        idle_sleep=args.idle_sleep,
        sync_every=args.sync_every,
        stop_on_hit=not args.no_stop_on_hit,
        timeout=_loop_timeout(args),
        sync=not args.no_sync,
        status=args.status,
        bits_min=args.bits_min,
        bits_max=args.bits_max,
        puzzle_ids=_loop_ids(args),
        limit=args.limit,
        resource=args.resource,
        require_doctor=not args.no_doctor,
        audit=not args.no_audit,
        check_balance=args.balance,
        transfer=not args.no_transfer,
        notify=not args.no_notify,
        progress=not args.no_progress,
        plan_path=_loop_plan_path(args),
    )
    print(format_watch_result(result))
    if result.last is not None:
        for item in result.last.transfers:
            _print_transfer(item)
    if result.stopped_reason == "hit":
        return 0
    if result.last is not None and not result.last.ok:
        return 1
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    if args.live:
        print(
            "WARNING: --live authorises real BTC broadcasts on a hit "
            "(AUTO_TRANSFER_DRY_RUN=false).",
            file=sys.stderr,
        )
    shown = 0

    def emit(stage: Stage) -> None:
        nonlocal shown
        shown += 1
        print(stage.format(shown, len(STAGES)), flush=True)

    result = run_auto(
        args.puzzle,
        dest_addr=args.dest,
        notify_url=args.notify,
        telegram_token=args.telegram_token,
        telegram_chat=args.telegram_chat,
        live=args.live,
        relay_url=args.relay,
        relay_seal_pubkey=args.relay_seal_pubkey,
        relay_token=args.relay_token,
        sync=not args.no_sync,
        engine=args.engine,
        allow_cpu_fallback=args.allow_cpu_fallback,
        ignore_swept=args.ignore_swept,
        build=not args.no_build,
        install_deps=not args.no_install_deps,
        selfcheck=not args.no_selfcheck,
        selfcheck_timeout=args.selfcheck_timeout,
        dp=args.dp,
        threads=args.threads,
        plan_only=args.plan_only,
        max_hours=args.max_hours,
        max_passes=args.max_passes,
        max_seconds=args.max_seconds,
        progress=not args.no_progress,
        on_stage=emit,
    )
    if result.watch is not None:
        print()
        print(format_watch_result(result.watch))
        if result.watch.last is not None:
            for item in result.watch.last.transfers:
                _print_transfer(item)
    if result.message:
        print()
        print(result.message)
    if result.ok:
        return 0
    return 2 if result.failed_stage == "config" else 1


def _add_dest_notify_relay_args(
    parser: argparse.ArgumentParser, *, notify_short: bool = False
) -> None:
    """Flags shared by `auto` and `config` — dest/notify on control, relay on hunt."""
    parser.add_argument(
        "--dest",
        default=None,
        help=(
            "payout address stored in config/.env (control VPS / standalone). "
            "Do not set this together with --relay"
        ),
    )
    notify_flags = ["--notify", "-n"] if notify_short else ["--notify"]
    parser.add_argument(
        *notify_flags,
        default=None,
        dest="notify",
        help="alert webhook URL (Discord / Slack / ntfy / custom), stored in config/.env",
    )
    parser.add_argument(
        "--telegram-token",
        default=None,
        help="Telegram bot token (needs --telegram-chat)",
    )
    parser.add_argument(
        "--telegram-chat",
        default=None,
        help="Telegram chat id (needs --telegram-token)",
    )
    parser.add_argument(
        "--relay",
        default=None,
        help="control hub URL, e.g. https://<control>:8787/hit (hunt boxes; no dest)",
    )
    parser.add_argument(
        "--relay-seal-pubkey",
        default=None,
        help="X25519 pubkey hex from `relay-keygen` on the control VPS",
    )
    parser.add_argument(
        "--relay-token",
        default=None,
        help="shared bearer token for the control hub (never re-printed after write)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc-puzzle-lab",
        description="Practice lab for Bitcoin Puzzle Transaction workflows",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_auto = sub.add_parser(
        "auto",
        help=(
            "one command: pick the engine for this host, build it, and hunt "
            "one puzzle unattended"
        ),
        description=(
            "Configure once with --dest / --notify, then `auto <id>` probes the host, "
            "picks the right solver, installs its build dependencies, clones and "
            "compiles it at a pinned commit, verifies it against a known answer, and "
            "runs the watch loop. A hit is audited, swept (dry-run unless --live) and "
            "announced on the notify channel."
        ),
    )
    p_auto.add_argument("puzzle", type=int, help="puzzle id to hunt, e.g. 140")
    _add_dest_notify_relay_args(p_auto)
    p_auto.add_argument(
        "--live",
        action="store_true",
        help=(
            "authorise real broadcasts on a hit. Without it a sweep is signed to "
            "state/dryrun_*.txhex and never sent."
        ),
    )
    p_auto.add_argument(
        "--engine",
        choices=_ENGINE_CHOICES,
        default=None,
        help="pin the engine instead of choosing one from the host profile",
    )
    p_auto.add_argument(
        "--allow-cpu-fallback",
        action="store_true",
        help="if the GPU has no CUDA toolkit, run the CPU engine instead of stopping",
    )
    p_auto.add_argument(
        "--ignore-swept",
        action="store_true",
        help="search even when the target's prize has already been claimed",
    )
    p_auto.add_argument(
        "--plan-only",
        action="store_true",
        help="show the engine decision and stop before building or searching",
    )
    p_auto.add_argument(
        "--no-sync",
        action="store_true",
        help="skip the catalog import and use the workspace catalog as-is",
    )
    p_auto.add_argument(
        "--no-build",
        action="store_true",
        help="assume the solver is already installed",
    )
    p_auto.add_argument(
        "--no-install-deps",
        action="store_true",
        help="do not apt/dnf install missing compilers and headers",
    )
    p_auto.add_argument(
        "--no-selfcheck",
        action="store_true",
        help="skip solving a known-answer puzzle after the build",
    )
    p_auto.add_argument(
        "--selfcheck-timeout",
        type=float,
        default=180.0,
        help="self-check budget in seconds (default: 180)",
    )
    p_auto.add_argument(
        "--dp",
        type=int,
        default=None,
        help="override kangaroo distinguished-point bits (default: derived, 30)",
    )
    p_auto.add_argument(
        "--threads",
        type=int,
        default=None,
        help="override solver threads (default: from the host tier)",
    )
    p_auto.add_argument("--max-hours", type=float, default=None, help="stop after N hours")
    p_auto.add_argument("--max-passes", type=int, default=None, help="stop after N passes")
    p_auto.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="recycle the solver after N seconds per pass",
    )
    p_auto.add_argument("--no-progress", action="store_true", help="quiet solver progress")
    p_auto.set_defaults(func=cmd_auto)

    p_config = sub.add_parser(
        "config",
        help="persist dest / notify / relay without starting a search",
    )
    _add_dest_notify_relay_args(p_config, notify_short=True)
    p_config.add_argument(
        "--new-relay-token",
        action="store_true",
        help="generate a RELAY_TOKEN and print it once",
    )
    p_config.set_defaults(func=cmd_config)

    p_keygen = sub.add_parser(
        "relay-keygen",
        help="create a seal keypair on the control VPS; copy only the pubkey to hunt boxes",
    )
    p_keygen.set_defaults(func=cmd_relay_keygen)

    p_unseal = sub.add_parser(
        "unseal",
        help="decrypt a sealed solution (prints the key only with --show-key)",
    )
    p_unseal.add_argument("token", nargs="?", help="bpl1. token, or a pasted relay message")
    p_unseal.add_argument("--file", default=None, help="read the token from a file")
    p_unseal.add_argument(
        "--show-key",
        action="store_true",
        help="print the recovered private key",
    )
    p_unseal.set_defaults(func=cmd_unseal)

    p_flush = sub.add_parser(
        "relay-flush",
        help="retry undelivered relay outbox rows",
    )
    p_flush.set_defaults(func=cmd_relay_flush)

    p_hub = sub.add_parser(
        "hub",
        help="control VPS: receive sealed hits, unseal, notify, sweep",
    )
    p_hub.add_argument(
        "--host",
        default="0.0.0.0",
        help="bind address (default 0.0.0.0; put TLS/firewall in front)",
    )
    p_hub.add_argument("--port", type=int, default=8787, help="listen port (default 8787)")
    p_hub.add_argument(
        "--no-sweep",
        action="store_true",
        help="record + notify only (do not call sweep_hit)",
    )
    p_hub.add_argument(
        "--no-notify",
        action="store_true",
        help="record + sweep only (do not post Discord/Telegram)",
    )
    p_hub.set_defaults(func=cmd_hub)

    p_list = sub.add_parser("list", help="list puzzles in the active catalog")
    p_list.set_defaults(func=cmd_list)

    p_import = sub.add_parser(
        "import-catalog",
        help="import full ~160-puzzle catalog into data/puzzles.json",
    )
    p_import.add_argument(
        "--url",
        default=None,
        help=(
            "download CSV from URL instead of the bundled export "
            f"(example: {DEFAULT_EXPORT_URL})"
        ),
    )
    p_import.add_argument(
        "--from-csv",
        default=None,
        help="read a local CSV export (overrides bundled / --url)",
    )
    p_import.add_argument(
        "--output",
        default=None,
        help="output puzzles.json path (default: <workspace>/data/puzzles.json)",
    )
    p_import.add_argument(
        "--no-solutions",
        action="store_true",
        help="omit practice_solution_hex even for publicly solved puzzles",
    )
    p_import.set_defaults(func=cmd_import_catalog)

    p_plan = sub.add_parser("plan", help="build catalog-wide batch plan (algorithm routing)")
    p_plan.add_argument(
        "--status",
        choices=["all", "solved", "unsolved"],
        default="all",
        help="filter catalog status (default: all)",
    )
    p_plan.add_argument("--bits-min", type=int, default=None, help="min bits inclusive")
    p_plan.add_argument("--bits-max", type=int, default=None, help="max bits inclusive")
    p_plan.add_argument(
        "--ids",
        default=None,
        help="comma-separated puzzle ids, e.g. 1,5,20,71",
    )
    p_plan.add_argument(
        "--output",
        "--plan",
        dest="output",
        default=None,
        help="plan path (default: state/batch_plan.json)",
    )
    p_plan.add_argument(
        "--verbose",
        action="store_true",
        help="print every job row",
    )
    p_plan.set_defaults(func=cmd_plan)

    p_batch = sub.add_parser("batch", help="execute jobs from a batch plan")
    p_batch.add_argument(
        "--plan",
        default=None,
        help="plan path (default: state/batch_plan.json)",
    )
    p_batch.add_argument(
        "--limit",
        type=int,
        default=None,
        help="run at most N ready jobs this invocation",
    )
    p_batch.add_argument(
        "--no-resume",
        action="store_true",
        help="do not skip jobs already marked done/hit",
    )
    p_batch.add_argument(
        "--stop-on-hit",
        action="store_true",
        help="stop the batch after the first new hit",
    )
    p_batch.add_argument(
        "--include-blocked",
        action="store_true",
        help="retry blocked jobs if their engine binary became available",
    )
    p_batch.add_argument(
        "--no-progress",
        action="store_true",
        help="disable per-job search progress output",
    )
    p_batch.set_defaults(func=cmd_batch)

    p_status = sub.add_parser("status", help="show batch plan × coverage × hits matrix")
    p_status.add_argument(
        "--plan",
        default=None,
        help="plan path (default: state/batch_plan.json)",
    )
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="verify catalog solution derives the address")
    p_verify.add_argument("puzzle", type=int, help="puzzle id, e.g. 20")
    p_verify.set_defaults(func=cmd_verify)

    p_strategy = sub.add_parser(
        "strategy",
        help="show inventory-aware engine plan (what can run now; not the `auto` command)",
    )
    p_strategy.add_argument("puzzle", type=int, help="puzzle id, e.g. 20")
    p_strategy.set_defaults(func=cmd_strategy)

    p_engines = sub.add_parser(
        "engines",
        help="list or install external solver toolchain (keyhunt/kangaroo/bitcrack)",
    )
    eng_sub = p_engines.add_subparsers(dest="engines_action")
    p_eng_status = eng_sub.add_parser("status", help="show solver binary status (default)")
    p_eng_status.set_defaults(func=cmd_engines, engines_action="status")
    p_eng_install = eng_sub.add_parser(
        "install",
        help="clone+build upstream solvers into workspace bin/ (production path)",
    )
    p_eng_install.add_argument(
        "--only",
        default=None,
        help=(
            "comma-separated: keyhunt,kangaroo,bitcrack,rckangaroo "
            "(default: CPU pair, plus the GPU pair when nvcc is present)"
        ),
    )
    p_eng_install.add_argument(
        "--force",
        action="store_true",
        help="rebuild even if bin/<engine> already exists",
    )
    p_eng_install.add_argument(
        "--no-selfcheck",
        action="store_true",
        help="skip the post-install solve check (leaves engines unverified)",
    )
    p_eng_install.add_argument(
        "--selfcheck-timeout",
        type=float,
        default=180.0,
        help="per-engine self-check budget in seconds (default: 180)",
    )
    p_eng_install.set_defaults(func=cmd_engines, engines_action="install")

    p_eng_check = eng_sub.add_parser(
        "selfcheck",
        # Local-only on purpose: this searches keyspace, which must never run on
        # GitHub-hosted runners. See the note in .github/workflows/ci.yml.
        help="verify installed solvers by solving puzzles with known answers",
    )
    p_eng_check.add_argument(
        "--only",
        default=None,
        help="comma-separated engines to verify (default: every installed engine)",
    )
    p_eng_check.add_argument(
        "--selfcheck-timeout",
        type=float,
        default=180.0,
        help="per-engine budget in seconds (default: 180)",
    )
    p_eng_check.set_defaults(func=cmd_engines, engines_action="selfcheck")
    p_engines.set_defaults(func=cmd_engines, engines_action="status")

    p_host = sub.add_parser("host", help="probe host profile (CPU/RAM/GPU/engines/tier)")
    p_host.set_defaults(func=cmd_host)

    p_adapt = sub.add_parser(
        "adapt",
        help="show environment-adaptive profile and recommended next actions",
    )
    p_adapt.set_defaults(func=cmd_adapt)

    p_doctor = sub.add_parser(
        "doctor",
        help="preflight checks before a machine experiment session",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    p_run = sub.add_parser("run", help="search / practice-run a puzzle and append HITS")
    p_run.add_argument("puzzle", type=int, help="puzzle id, e.g. 20")
    p_run.add_argument(
        "--auto",
        action="store_true",
        help=(
            "pick engine from solvers already installed (plan_strategy). "
            "Unattended hunt with build-if-needed is the `auto` command"
        ),
    )
    p_run.add_argument(
        "--engine",
        choices=_ENGINE_CHOICES,
        default=None,
        help="override catalog default engine (also overrides --auto engine)",
    )
    p_run.add_argument(
        "--window",
        type=int,
        default=1_000_000,
        help="keys in practice window for --engine window (default: 1000000)",
    )
    p_run.add_argument(
        "--threads",
        type=int,
        default=2,
        help="threads for keyhunt/kangaroo (default: 2)",
    )
    p_run.add_argument(
        "--dp",
        type=int,
        default=None,
        help="RCKangaroo DP bits (default: adaptive with --auto, else 16)",
    )
    p_run.add_argument(
        "--workers",
        type=int,
        default=1,
        help="process workers for sequential/window scan (default: 1)",
    )
    p_run.add_argument(
        "--resume",
        action="store_true",
        help="resume from state/scan_<id>.json checkpoint if present",
    )
    p_run.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help="stop an external solver after this many seconds (keyhunt does not self-exit)",
    )
    p_run.add_argument(
        "--coverage",
        action="store_true",
        help="scan via coverage ledger chunks (skips already-done ranges)",
    )
    p_run.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"coverage chunk size in keys (default: {DEFAULT_CHUNK_SIZE})",
    )
    p_run.add_argument(
        "--order",
        choices=["sequential", "random"],
        default="sequential",
        help="coverage chunk order (default: sequential)",
    )
    p_run.add_argument(
        "--seed",
        type=int,
        default=None,
        help="RNG seed for --order random (reproducible plans)",
    )
    p_run.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="scan at most N pending/in-progress chunks this run",
    )
    p_run.add_argument(
        "--no-progress",
        action="store_true",
        help="disable progress / keys-per-second output",
    )
    p_run.add_argument(
        "--show-key",
        action="store_true",
        help="print private key hex on hit (off by default)",
    )
    p_run.add_argument(
        "--transfer",
        action="store_true",
        help="after a hit, attempt sweep transfer (respects AUTO_TRANSFER_* gates)",
    )
    p_run.set_defaults(func=cmd_run)

    p_cov = sub.add_parser("coverage", help="show range coverage ledger status")
    p_cov.add_argument(
        "puzzle",
        type=int,
        nargs="?",
        default=None,
        help="puzzle id (default: list all local coverage ledgers)",
    )
    p_cov.set_defaults(func=cmd_coverage)

    p_audit = sub.add_parser("audit", help="verify recorded HITS locally")
    p_audit.add_argument(
        "--balance",
        action="store_true",
        help="also query mempool.space for address balance",
    )
    p_audit.add_argument(
        "--export",
        type=str,
        default=None,
        help="write JSON audit report to this path (no private keys)",
    )
    p_audit.set_defaults(func=cmd_audit)

    p_transfer = sub.add_parser(
        "transfer",
        help="sweep a recorded hit (default dry-run; requires AUTO_TRANSFER_*)",
    )
    p_transfer.add_argument(
        "--puzzle",
        type=int,
        default=None,
        help="puzzle id to sweep (default: latest hit)",
    )
    p_transfer.add_argument(
        "--fee-rate",
        type=int,
        default=None,
        help="override fee rate sat/vB for this sweep",
    )
    p_transfer.add_argument(
        "--allow-unconfirmed",
        action="store_true",
        help="include unconfirmed UTXOs (default: confirmed only)",
    )
    p_transfer.add_argument(
        "--broadcast-dry-run",
        type=str,
        default=None,
        help="broadcast an existing dry-run .txhex (requires live confirm + dry_run=false)",
    )
    p_transfer.add_argument(
        "--verify-dry-run",
        action="store_true",
        help="after dry-run, structurally verify the written .txhex artifact",
    )
    p_transfer.set_defaults(func=cmd_transfer)

    p_vdr = sub.add_parser(
        "verify-dry-run",
        help="structurally verify a dry-run .txhex file (never prints hex)",
    )
    p_vdr.add_argument("path", type=str, help="path to dryrun_*.txhex")
    p_vdr.add_argument(
        "--check-dest",
        action="store_true",
        help="also assert output matches AUTO_TRANSFER_DEST_ADDR / min send",
    )
    p_vdr.set_defaults(func=cmd_verify_dry_run)

    p_summary = sub.add_parser("summary", help="show local pipeline summary")
    p_summary.add_argument(
        "--recent",
        type=int,
        default=10,
        help="number of recent run-log events to show (default: 10)",
    )
    p_summary.set_defaults(func=cmd_summary)

    def _add_loop_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--no-sync",
            action="store_true",
            help="skip catalog import (use current workspace catalog)",
        )
        parser.add_argument(
            "--status",
            choices=["all", "solved", "unsolved"],
            default="unsolved",
            help="catalog status filter for planning (default: unsolved)",
        )
        parser.add_argument("--bits-min", type=int, default=32)
        parser.add_argument("--bits-max", type=int, default=None)
        parser.add_argument(
            "--plan-file",
            type=str,
            default=None,
            help=(
                "job board path (default: state/batch_plan.json). Give concurrent "
                "loops separate files so they do not overwrite each other's plan."
            ),
        )
        parser.add_argument(
            "--ids",
            type=str,
            default=None,
            help="comma-separated puzzle ids (e.g. 71)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=1,
            help="max puzzles for this host pass (default: 1 = exclusive slot)",
        )
        parser.add_argument(
            "--resource",
            choices=["auto", "cpu", "gpu", "any"],
            default="auto",
            help="resource queue (default: auto → gpu on GPU hosts)",
        )
        parser.add_argument(
            "--no-stop-on-hit",
            action="store_true",
            help="keep going after a hit within --limit",
        )
        parser.add_argument(
            "--no-doctor",
            action="store_true",
            help="skip blocking doctor gate",
        )
        parser.add_argument(
            "--no-audit",
            action="store_true",
            help="skip post-hit address verification",
        )
        parser.add_argument(
            "--balance",
            action="store_true",
            help="also query chain balance during audit",
        )
        parser.add_argument(
            "--no-transfer",
            action="store_true",
            help="skip sweep attempt (still records hits)",
        )
        parser.add_argument(
            "--no-notify",
            action="store_true",
            help="skip hit webhook/Telegram notify (NOTIFY_* in config/.env)",
        )
        parser.add_argument(
            "--no-progress",
            action="store_true",
            help="quiet search progress",
        )
        parser.add_argument(
            "--max-seconds",
            type=float,
            default=None,
            help="stop an external solver after N seconds (SIGTERM)",
        )

    p_once = sub.add_parser(
        "once",
        help=(
            "full loop: sync unsolved → plan → one resource slot → "
            "audit → optional dry-run transfer"
        ),
    )
    _add_loop_args(p_once)
    p_once.set_defaults(func=cmd_once)

    p_watch = sub.add_parser(
        "watch",
        help="repeat once until hit / max-hours / max-passes / idle",
    )
    _add_loop_args(p_watch)
    p_watch.add_argument(
        "--max-hours",
        type=float,
        default=None,
        help="stop watch after this many hours (also caps solver timeout)",
    )
    p_watch.add_argument(
        "--max-passes",
        type=int,
        default=None,
        help="stop after N once passes",
    )
    p_watch.add_argument(
        "--idle-sleep",
        type=float,
        default=30.0,
        help="seconds to sleep when no ready job (default: 30)",
    )
    p_watch.add_argument(
        "--sync-every",
        type=int,
        default=1,
        help="import-catalog every N passes (default: 1)",
    )
    p_watch.set_defaults(func=cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.func(args))
    except KeyError as exc:
        detail = exc.args[0] if exc.args else str(exc)
        print(f"error: {detail}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
