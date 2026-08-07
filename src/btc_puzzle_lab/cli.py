from __future__ import annotations

import argparse
import sys

from btc_puzzle_lab import __version__
from btc_puzzle_lab.audit import audit_hits
from btc_puzzle_lab.catalog import get_puzzle, load_puzzles
from btc_puzzle_lab.crypto import privkey_bytes, privkey_to_p2pkh_address
from btc_puzzle_lab.hits import read_hits
from btc_puzzle_lab.paths import HITS_FILE
from btc_puzzle_lab.search import run_puzzle


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
    )
    print(f"engine={outcome.engine}: {outcome.message}")
    if outcome.hit is None:
        return 1
    print(f"hit puzzle #{outcome.hit.puzzle_id} address={outcome.hit.address}")
    print(f"recorded in {HITS_FILE}")
    # Intentionally do not print private keys to stdout by default.
    if args.show_key:
        print(f"private_key_hex={outcome.hit.private_key_hex}")
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
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc-puzzle-lab",
        description="Practice lab for solved Bitcoin Puzzle Transaction entries",
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
        "--show-key",
        action="store_true",
        help="print private key hex on hit (off by default)",
    )
    p_run.set_defaults(func=cmd_run)

    p_audit = sub.add_parser("audit", help="verify recorded HITS locally")
    p_audit.add_argument(
        "--balance",
        action="store_true",
        help="also query mempool.space for address balance",
    )
    p_audit.set_defaults(func=cmd_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
