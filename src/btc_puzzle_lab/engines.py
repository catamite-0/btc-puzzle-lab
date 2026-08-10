"""Adapters for external search binaries.

Lab builds argv, runs the process, parses a private key int, and returns.

Production path: ``btc-puzzle-lab engines install`` clones/builds upstream
solvers into workspace ``bin/`` and writes ``config/engines.env``.
Manual ``*_PATH`` env vars still win when set.
"""

from __future__ import annotations

import os
import random
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from btc_puzzle_lab.catalog import Puzzle
from btc_puzzle_lab.crypto import normalize_privkey_hex
from btc_puzzle_lab.paths import workspace_root

_HEX_KEY = re.compile(r"\b(0x)?([0-9a-fA-F]{1,64})\b")
_PRIVATE_KEY_LINE_RE = re.compile(
    r"(?i)((?:private\s*key|privkey|priv)\s*:?\s*)(?:0x)?[0-9a-f]{1,64}"
)
# BitCrack result files carry no label: "<address> <privkey> <pubkey>".
_BITCRACK_FOUND_RE = re.compile(
    r"^\s*(?:[13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[023456789ac-hj-np-z]{11,71})\s+"
    r"([0-9a-fA-F]{64})\s+"
    r"(?:0[23][0-9a-fA-F]{64}|04[0-9a-fA-F]{128})\s*$"
)


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
    for line in text.splitlines():
        match = _BITCRACK_FOUND_RE.match(line)
        if match:
            try:
                return int(normalize_privkey_hex(match.group(1)), 16)
            except ValueError:
                continue
    return None


def redact_engine_line(line: str) -> str:
    """Strip plaintext private-key material before printing solver logs."""
    return _PRIVATE_KEY_LINE_RE.sub(r"\1[REDACTED]", line)


def _append_result_files(cwd: Path, output: str) -> str:
    for name in (
        "RESULTS.TXT",
        "Result.txt",
        "KEYFOUND.key",
        "KEYFOUNDKEYFOUND.txt",  # keyhunt's actual hit file
        "Found.txt",
        "found.txt",
    ):
        path = cwd / name
        if path.is_file():
            output += "\n" + path.read_text(encoding="utf-8", errors="ignore")
    return output


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: float | None = None,
    progress: bool = True,
) -> tuple[int, str]:
    """Stream solver output (redacted) so long GPU runs do not buffer forever."""
    print("running:", " ".join(cmd), flush=True)
    chunks: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        return 127, f"failed to start solver: {exc}"

    assert proc.stdout is not None
    deadline = time.monotonic() + timeout if timeout is not None and timeout > 0 else None
    timed_out = False
    solved_early = False
    next_result_poll = time.monotonic() + 1.0
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            # keyhunt keeps scanning after it writes the key, so waiting for the
            # process to exit burns the whole budget on a solved puzzle.
            if time.monotonic() >= next_result_poll:
                next_result_poll = time.monotonic() + 1.0
                if parse_privkey_text(_append_result_files(cwd, "")) is not None:
                    solved_early = True
                    break
            wait = 0.25
            if deadline is not None:
                wait = max(0.01, min(wait, deadline - time.monotonic()))
            events = selector.select(timeout=wait)
            if not events:
                if proc.poll() is not None:
                    # Drain any trailing bytes after exit.
                    rest = proc.stdout.read()
                    if rest:
                        chunks.append(rest)
                        if progress:
                            print(redact_engine_line(rest), end="", flush=True)
                    break
                continue
            line = proc.stdout.readline()
            if line == "":
                if proc.poll() is not None:
                    break
                continue
            chunks.append(line)
            if progress:
                print(redact_engine_line(line), end="", flush=True)
        if (timed_out or solved_early) and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)
    finally:
        selector.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)

    output = "".join(chunks)
    output = _append_result_files(cwd, output)
    if solved_early:
        # We killed a winning solver on purpose; its exit status is meaningless.
        code = 0
        output += "\n[btc-puzzle-lab] engine stopped: key found in result file"
    elif timed_out:
        code = 124
        output += "\n[btc-puzzle-lab] engine stopped: timeout reached"
    else:
        code = int(proc.returncode if proc.returncode is not None else 1)
    return code, output


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
    # RCKangaroo loads its kernel_sm*.cubin from the working directory. Without them it
    # does not exit — it spins at "Speed: 0 MKeys/s, Err: 1" forever — so link them in.
    for cubin in sorted(binary.parent.glob("*.cubin")):
        try:
            (tmp / cubin.name).symlink_to(cubin)
        except OSError:
            shutil.copy2(cubin, tmp / cubin.name)
    # RCKangaroo: -range is bit-width of interval (= bits-1), -start is range_start.
    cmd = [
        str(binary),
        "-dp",
        # Upstream accepts 14..32; anything higher is rejected outright.
        str(max(14, min(dp, 32))),
        "-range",
        str(max(32, puzzle.bits - 1)),
        "-start",
        f"{puzzle.range_start:x}",
        "-pubkey",
        puzzle.pubkey_compressed_hex,
    ]
    return cmd, tmp


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw, 0) if raw else None
    except ValueError:
        return None


def bitcrack_keyspace(puzzle: Puzzle) -> str:
    """Sequential full range, or a random window when random mode is on.

    BitCrack only scans forward from a start key, so "random scan" means drawing a
    fresh uniform start per invocation instead of replaying the low end every time.
    """
    if not _env_flag("BTC_PUZZLE_LAB_BITCRACK_RANDOM"):
        return f"{puzzle.range_start:x}:{puzzle.range_end:x}"
    span = puzzle.range_end - puzzle.range_start + 1
    chunk = _env_int("BTC_PUZZLE_LAB_BITCRACK_CHUNK") or (1 << 40)
    chunk = max(1, min(chunk, span))
    start = puzzle.range_start + random.SystemRandom().randrange(span - chunk + 1)
    return f"{start:x}:+{chunk:x}"


def _cmd_bitcrack(binary: Path, puzzle: Puzzle) -> tuple[list[str], Path]:
    tmp = Path(tempfile.mkdtemp(prefix="btc-puzzle-lab-bc-"))
    out = tmp / "found.txt"
    keyspace = bitcrack_keyspace(puzzle)
    cmd = [str(binary), "-c"]
    # Optional device / grid knobs for VPS tuning (safe defaults when unset).
    device = os.environ.get("BTC_PUZZLE_LAB_GPU_INDEX", "").strip()
    blocks = os.environ.get("BTC_PUZZLE_LAB_BITCRACK_BLOCKS", "").strip()
    threads = os.environ.get("BTC_PUZZLE_LAB_BITCRACK_THREADS", "").strip()
    points = os.environ.get("BTC_PUZZLE_LAB_BITCRACK_POINTS", "").strip()
    if device.isdigit():
        cmd += ["-d", device]
    if blocks.isdigit():
        cmd += ["-b", blocks]
    if threads.isdigit():
        cmd += ["-t", threads]
    if points.isdigit():
        cmd += ["-p", points]
    cmd += ["--keyspace", keyspace, "-o", str(out), puzzle.address]
    return cmd, tmp


def run_external_engine(
    puzzle: Puzzle,
    engine: str,
    *,
    threads: int = 2,
    dp: int = 16,
    timeout: float | None = None,
    progress: bool = True,
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
        code, output = _run(cmd, cwd=cwd, timeout=timeout, progress=progress)
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
        detail = f"{engine} exited {code}; no private key parsed"
        if code == 124:
            detail = f"{engine} timed out; no private key parsed"
        return ExternalEngineResult(engine, None, detail, tuple(cmd))
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
        elif name in {"keyhunt", "kangaroo", "bitcrack"}:
            shown = f"(run: btc-puzzle-lab engines install --only {name})"
        else:
            shown = f"(manual: set {spec.env_var})"
        lines.append(f"{name:<12}  {mark:<9}  {shown}")
    return "\n".join(lines)
