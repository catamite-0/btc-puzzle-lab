"""Adapters for external search binaries.

Lab builds argv, runs the process, parses a private key int, and returns.

Production path: ``btc-puzzle-lab engines install`` clones/builds upstream
solvers into workspace ``bin/`` and writes ``config/engines.env``.
Manual ``*_PATH`` env vars still win when set.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.crypto import normalize_privkey_hex
from btc_puzzle_lab.paths import workspace_root

_HEX_KEY = re.compile(r"\b(0x)?([0-9a-fA-F]{1,64})\b")


@dataclass(frozen=True)
class EngineBinary:
    name: str
    env_var: str
    candidates: tuple[str, ...]
    needs_pubkey: bool
    min_bits: int = 1


ENGINES: dict[str, EngineBinary] = {
    "keyhunt": EngineBinary(
        name="keyhunt",
        env_var="KEYHUNT_PATH",
        candidates=("bin/keyhunt",),
        needs_pubkey=False,
    ),
    "bitcrack": EngineBinary(
        name="bitcrack",
        env_var="BITCRACK_PATH",
        candidates=(
            "bin/cuBitCrack",
            "bin/clBitCrack",
            "bin/BitCrack",
        ),
        needs_pubkey=False,
    ),
    "kangaroo": EngineBinary(
        name="kangaroo",
        env_var="KANGAROO_PATH",
        candidates=(
            "bin/kangaroo",
            "bin/Kangaroo",
        ),
        needs_pubkey=True,
        min_bits=32,
    ),
    "rckangaroo": EngineBinary(
        name="rckangaroo",
        env_var="RCKANGAROO_PATH",
        candidates=(
            "bin/RCKangaroo",
            "bin/rckangaroo",
        ),
        needs_pubkey=True,
        min_bits=32,
    ),
}


@dataclass(frozen=True)
class ExternalEngineResult:
    engine: str
    secret: int | None
    message: str
    cmdline: tuple[str, ...] = ()


def load_engine_env() -> None:
    """Load workspace config/engines.env without overriding exported vars."""
    env_path = workspace_root() / "config" / "engines.env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)


def resolve_binary(name: str) -> Path | None:
    load_engine_env()
    spec = ENGINES[name]
    env = os.environ.get(spec.env_var)
    paths: list[Path] = []
    if env:
        paths.append(Path(env).expanduser())
    # Workspace-managed toolchain only (respects BTC_PUZZLE_LAB_HOME).
    # Do not fall back to process-cwd bin/ — that leaks host checkouts into tests
    # and alternate homes.
    for candidate in spec.candidates:
        paths.append(workspace_root() / "bin" / Path(candidate).name)
    for path in paths:
        resolved = path.resolve() if path.exists() else path
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    return None


def available_engines() -> list[str]:
    return [name for name in ENGINES if resolve_binary(name) is not None]


def parse_privkey_text(text: str) -> int | None:
    """Extract a private key int from solver stdout/stderr/result files."""
    for line in text.splitlines():
        lower = line.lower()
        if not any(token in lower for token in ("private key", "privkey", "priv:", "priv ")):
            continue
        for match in _HEX_KEY.finditer(line.replace(":", " ")):
            token = match.group(2)
            try:
                return int(normalize_privkey_hex(token), 16)
            except ValueError:
                continue
    return None


def _run(cmd: list[str], *, cwd: Path) -> tuple[int, str]:
    print("running:", " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Some solvers also write RESULTS.TXT / similar beside cwd.
    for name in ("RESULTS.TXT", "Result.txt", "KEYFOUND.key", "Found.txt", "found.txt"):
        path = cwd / name
        if path.is_file():
            output += "\n" + path.read_text(encoding="utf-8", errors="ignore")
    return proc.returncode, output


def _cmd_keyhunt(binary: Path, puzzle: Puzzle, *, threads: int) -> tuple[list[str], Path]:
    tmp = Path(tempfile.mkdtemp(prefix="btc-puzzle-lab-kh-"))
    target = tmp / "target.txt"
    target.write_text(puzzle.address + "\n", encoding="utf-8")
    cmd = [
        str(binary),
        "-m",
        "address",
        "-f",
        str(target),
        "-b",
        str(puzzle.bits),
        "-l",
        "compress",
        "-t",
        str(max(1, threads)),
        "-s",
        "5",
        "-q",
    ]
    return cmd, tmp


def _cmd_kangaroo(binary: Path, puzzle: Puzzle, *, threads: int) -> tuple[list[str], Path]:
    tmp = Path(tempfile.mkdtemp(prefix="btc-puzzle-lab-kg-"))
    work = tmp / "work.txt"
    work.write_text(
        f"{puzzle.range_start:x}\n{puzzle.range_end:x}\n{puzzle.pubkey_compressed_hex}\n",
        encoding="utf-8",
    )
    cmd = [str(binary), "-t", str(max(1, threads)), str(work)]
    return cmd, tmp


def _cmd_rckangaroo(binary: Path, puzzle: Puzzle, *, dp: int) -> tuple[list[str], Path]:
    tmp = Path(tempfile.mkdtemp(prefix="btc-puzzle-lab-rc-"))
    # RCKangaroo: -range is bit-width of interval (= bits-1), -start is range_start.
    cmd = [
        str(binary),
        "-dp",
        str(max(14, min(dp, 60))),
        "-range",
        str(max(32, puzzle.bits - 1)),
        "-start",
        f"{puzzle.range_start:x}",
        "-pubkey",
        puzzle.pubkey_compressed_hex,
    ]
    return cmd, tmp


def _cmd_bitcrack(binary: Path, puzzle: Puzzle) -> tuple[list[str], Path]:
    tmp = Path(tempfile.mkdtemp(prefix="btc-puzzle-lab-bc-"))
    out = tmp / "found.txt"
    keyspace = f"{puzzle.range_start:x}:{puzzle.range_end:x}"
    cmd = [
        str(binary),
        "-c",
        "--keyspace",
        keyspace,
        "-o",
        str(out),
        puzzle.address,
    ]
    return cmd, tmp


def run_external_engine(
    puzzle: Puzzle,
    engine: str,
    *,
    threads: int = 2,
    dp: int = 16,
) -> ExternalEngineResult:
    if engine not in ENGINES:
        return ExternalEngineResult(engine, None, f"unknown external engine: {engine}")
    spec = ENGINES[engine]
    if spec.needs_pubkey and not puzzle.pubkey_compressed_hex:
        return ExternalEngineResult(engine, None, f"{engine} requires pubkey_compressed_hex")
    if puzzle.bits < spec.min_bits:
        return ExternalEngineResult(
            engine,
            None,
            f"{engine} needs bits>={spec.min_bits}; puzzle has {puzzle.bits}",
        )
    binary = resolve_binary(engine)
    if binary is None:
        return ExternalEngineResult(
            engine,
            None,
            f"{engine} not found; run: btc-puzzle-lab engines install "
            f"(or set {spec.env_var} / place binary under bin/)",
        )

    builders = {
        "keyhunt": lambda: _cmd_keyhunt(binary, puzzle, threads=threads),
        "bitcrack": lambda: _cmd_bitcrack(binary, puzzle),
        "kangaroo": lambda: _cmd_kangaroo(binary, puzzle, threads=threads),
        "rckangaroo": lambda: _cmd_rckangaroo(binary, puzzle, dp=dp),
    }
    cmd, cwd = builders[engine]()
    try:
        code, output = _run(cmd, cwd=cwd)
    finally:
        # Best-effort cleanup; ignore failures on busy filesystems.
        for path in sorted(cwd.rglob("*"), reverse=True):
            try:
                path.unlink()
            except OSError:
                pass
        try:
            cwd.rmdir()
        except OSError:
            pass

    secret = parse_privkey_text(output)
    if secret is None:
        return ExternalEngineResult(
            engine,
            None,
            f"{engine} exited {code}; no private key parsed",
            tuple(cmd),
        )
    return ExternalEngineResult(engine, secret, "hit from external engine", tuple(cmd))


def format_engine_status() -> str:
    lines = [
        "engine        available  path",
        f"toolchain bin: {workspace_root() / 'bin'}",
        f"engines.env  : {workspace_root() / 'config' / 'engines.env'}",
        "",
    ]
    for name, spec in ENGINES.items():
        path = resolve_binary(name)
        mark = "yes" if path else "no"
        if path:
            shown = str(path)
        elif name in {"keyhunt", "kangaroo"}:
            shown = f"(run: btc-puzzle-lab engines install --only {name})"
        else:
            shown = f"(manual: set {spec.env_var})"
        lines.append(f"{name:<12}  {mark:<9}  {shown}")
    return "\n".join(lines)
