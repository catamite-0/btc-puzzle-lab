from __future__ import annotations

import argparse
import sys
from pathlib import Path

from btc_puzzle_lab import __version__
from btc_puzzle_lab.audit import audit_hits, export_audit_report
from btc_puzzle_lab.catalog import get_puzzle, load_puzzles
from btc_puzzle_lab.crypto import privkey_bytes, privkey_to_p2pkh_address
from btc_puzzle_lab.hits import read_hits
from btc_puzzle_lab.paths import HITS_FILE
from btc_puzzle_lab.search import run_puzzle
from btc_puzzle_lab.settings import get_transfer_settings, validate_transfer_settings
from btc_puzzle_lab.summary import build_summary, format_summary
from btc_puzzle_lab.transfer import TransferResult, sweep_hit, verify_dry_run_file


def _print_transfer(result: TransferResult) -> None:
    print(f"transfer[{result.status}]: {result.message}")
    if result.send_amount is not None:
        print(
            f"  send_amount={result.send_amount} sats fee={result.fee} "
            f"fee_rate={result.fee_rate}"
        )
    if result.input_count is not None:
        print(f"  inputs={result.input_count} rbf={result.rbf}")
    if result.tx_fingerprint:
        print(f"  tx_fingerprint={result.tx_fingerprint}")
    if result.dry_run_path:
        print(f"  dry_run_path={result.dry_run_path}")
    if result.txid:
        print(f"  txid={result.txid}")


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
    outcome = run_puzzle(
        puzzle,
        engine=args.engine,
        window=args.window,
        threads=args.threads,
        workers=args.workers,
        resume=args.resume,
        progress=not args.no_progress,
    )
    print(f"engine={outcome.engine}: {outcome.message}")
    if outcome.hit is None:
        return 1
    print(f"hit puzzle #{outcome.hit.puzzle_id} address={outcome.hit.address}")
    print(f"recorded in {HITS_FILE}")
    if args.show_key:
        print(f"private_key_hex={outcome.hit.private_key_hex}")
    if args.transfer:
        _print_transfer(sweep_hit(outcome.hit))
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
    result = sweep_hit(hit, settings=settings)
    _print_transfer(result)
    if result.status == "dry_run" and result.dry_run_path and args.verify_dry_run:
        verify = verify_dry_run_file(result.dry_run_path)
        print(
            f"dry-run verify: {'OK' if verify.ok else 'FAIL'} — {verify.message} "
            f"(inputs={verify.input_count} outputs={verify.output_count} "
            f"size={verify.size_bytes})"
        )
        if not verify.ok:
            return 1
    return 0 if result.status in {"dry_run", "broadcast", "skipped"} else 1


def cmd_verify_dry_run(args: argparse.Namespace) -> int:
    result = verify_dry_run_file(args.path)
    print(f"[{'OK' if result.ok else 'FAIL'}] {result.message}")
    print(f"  path={result.path}")
    if result.fingerprint:
        print(f"  fingerprint={result.fingerprint}")
    if result.version is not None:
        print(
            f"  version={result.version} inputs={result.input_count} "
            f"outputs={result.output_count} size_bytes={result.size_bytes}"
        )
    return 0 if result.ok else 1


def cmd_summary(args: argparse.Namespace) -> int:
    summary = build_summary(recent=args.recent)
    print(format_summary(summary))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc-puzzle-lab",
        description="Practice lab for Bitcoin Puzzle Transaction workflows",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list practice puzzles")
    p_list.set_defaults(func=cmd_list)

    p_verify = sub.add_parser("verify", help="verify catalog solution derives the address")
    p_verify.add_argument("puzzle", type=int, help="puzzle id, e.g. 20")
    p_verify.set_defaults(func=cmd_verify)

    p_run = sub.add_parser("run", help="search / practice-run a puzzle and append HITS")
    p_run.add_argument("puzzle", type=int, help="puzzle id, e.g. 20")
    p_run.add_argument(
        "--engine",
        choices=["sequential", "window", "inject-known", "keyhunt"],
        default=None,
        help="override catalog default engine",
    )
    p_run.add_argument(
        "--window",
        type=int,
        default=1_000_000,
        help="keys in practice window for --engine window (default: 1000000)",
    )
    p_run.add_argument("--threads", type=int, default=2, help="keyhunt threads")
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
    p_vdr.set_defaults(func=cmd_verify_dry_run)

    p_summary = sub.add_parser("summary", help="show local pipeline summary")
    p_summary.add_argument(
        "--recent",
        type=int,
        default=10,
        help="number of recent run-log events to show (default: 10)",
    )
    p_summary.set_defaults(func=cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
