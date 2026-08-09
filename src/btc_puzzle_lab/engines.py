"""Adapters for external search binaries.

Lab builds argv, runs the process, parses a private key int, and returns.

Production path: ``btc-puzzle-lab engines install`` clones/builds upstream
solvers into workspace ``bin/`` and writes ``config/engines.env``.
Manual ``*_PATH`` env vars still win when set.
"""

from __future__ import annotations

import os
import re
import selectors
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
_BARE_SECRET_RE = re.compile(r"(?i)\b(?:0x)?[0-9a-f]{64}\b")
_BITCRACK_RESULT_RE = re.compile(
    r"^(?P<address>\S+)\s+"
    r"(?P<private>[0-9a-fA-F]{64})\s+"
    r"(?P<public>(?:(?:02|03)[0-9a-fA-F]{64}|04[0-9a-fA-F]{128}))\s*$"
)
_SOLVER_ENV_ALLOWLIST = {
    "COMSPEC",
    "CUDA_HOME",
    "CUDA_PATH",
    "CUDA_VISIBLE_DEVICES",
    "GPU_DEVICE_ORDINAL",
    "HOME",
    "LANG",
    "LD_LIBRARY_PATH",
    "NVIDIA_DRIVER_CAPABILITIES",
    "NVIDIA_VISIBLE_DEVICES",
    "OMP_NUM_THREADS",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


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


def parse_privkey_text(text: str, *, expected_address: str | None = None) -> int | None:
    """Extract a private key int from solver stdout/stderr/result files."""
    for line in text.splitlines():
        lower = line.lower()
        if any(token in lower for token in ("private key", "privkey", "priv:", "priv ")):
            for match in _HEX_KEY.finditer(line.replace(":", " ")):
                token = match.group(2)
                try:
                    return int(normalize_privkey_hex(token), 16)
                except ValueError:
                    continue
        if expected_address is not None:
            match = _BITCRACK_RESULT_RE.fullmatch(line.strip())
            if match is not None and match.group("address") == expected_address:
                try:
                    return int(normalize_privkey_hex(match.group("private")), 16)
                except ValueError:
                    continue
    return None


def redact_engine_line(line: str) -> str:
    """Strip plaintext private-key material before printing solver logs."""
    line = _PRIVATE_KEY_LINE_RE.sub(r"\1[REDACTED]", line)
    return _BARE_SECRET_RE.sub("[REDACTED]", line)


def _solver_env() -> dict[str, str]:
    """Minimal runtime environment; never pass app/cloud credentials to solvers."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SOLVER_ENV_ALLOWLIST or key.upper().startswith("LC_")
    }


def _append_result_files(cwd: Path, output: str) -> str:
    for name in ("RESULTS.TXT", "Result.txt", "KEYFOUND.key", "Found.txt", "found.txt"):
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
            env=_solver_env(),
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
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
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
        if timed_out and proc.poll() is None:
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
    code = 124 if timed_out else int(proc.returncode if proc.returncode is not None else 1)
    if timed_out:
        output += "\n[btc-puzzle-lab] engine stopped: timeout reached"
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
    state_dir = workspace_root() / "state"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass
    continue_file = state_dir / f"bitcrack_{puzzle.id}.continue"
    keyspace = f"{puzzle.range_start:x}:{puzzle.range_end:x}"
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
    cmd += [
        "--continue",
        str(continue_file),
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

    secret = parse_privkey_text(output, expected_address=puzzle.address)
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
