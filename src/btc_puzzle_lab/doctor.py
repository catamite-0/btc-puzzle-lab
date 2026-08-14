"""Preflight checks before a machine experiment session."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from btc_puzzle_lab import __version__
from btc_puzzle_lab.engines import available_engines, format_engine_status, resolve_binary
from btc_puzzle_lab.paths import CONFIG_DIR, STATE_DIR, workspace_root
from btc_puzzle_lab.settings import (
    format_notify_policy,
    format_relay_policy,
    get_notify_settings,
    get_relay_settings,
    get_transfer_settings,
    validate_notify_settings,
    validate_relay_settings,
    validate_transfer_settings,
)
from btc_puzzle_lab.strategy import GPU_ENGINES, probe_host
from btc_puzzle_lab.toolchain import (
    cuda_available,
    detect_compute_cap,
    detect_cuda_home,
    missing_build_headers,
    missing_build_tools,
)
from btc_puzzle_lab.transfer import format_transfer_policy

_HARD = frozenset({"build_tools", "state_writable", "transfer_policy", "relay_policy"})


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor() -> list[Check]:
    checks: list[Check] = []
    root = workspace_root()
    checks.append(Check("version", True, f"btc-puzzle-lab {__version__}"))
    checks.append(Check("workspace", True, str(root)))

    missing = missing_build_tools()
    missing_headers = missing_build_headers()
    if missing or missing_headers:
        detail = f"missing: {', '.join(missing + missing_headers)}"
        packages = {"gmp.h": "libgmp-dev", "openssl/sha.h": "libssl-dev"}
        hint = " ".join(packages[h] for h in missing_headers if h in packages)
        if hint:
            detail += f" — apt install -y {hint}"
    else:
        detail = "ok"
    checks.append(Check("build_tools", not (missing or missing_headers), detail))

    cuda_ok = cuda_available()
    cuda_home = detect_cuda_home()
    cap = detect_compute_cap()
    detail = "nvcc present" if cuda_ok else "nvcc missing (CPU solvers still ok)"
    if cuda_home:
        detail += f"; CUDA_HOME={cuda_home}"
    if cap:
        detail += f"; compute_cap={cap}"
    if shutil.which("nvidia-smi"):
        detail += "; nvidia-smi ok"
    checks.append(Check("cuda", True, detail))

    host = probe_host()
    checks.append(
        Check(
            "host",
            True,
            f"tier={host.tier} cpus={host.cpus} mem_mb={host.mem_mb} gpu={host.gpu}",
        )
    )

    engines = available_engines()
    checks.append(
        Check(
            "engines",
            True,
            ", ".join(engines)
            if engines
            else "none yet — next: btc-puzzle-lab engines install",
        )
    )
    for name in ("keyhunt", "kangaroo", "bitcrack", "rckangaroo"):
        path = resolve_binary(name)
        checks.append(
            Check(
                f"engine:{name}",
                True,
                str(path) if path else "missing",
            )
        )

    # GPU hosts should surface a missing solver before `once` burns idle hours.
    # Tier follows the card, not installed binaries; compute is high-CPU/no-card.
    if host.gpu or host.tier == "gpu":
        found = [name for name in sorted(GPU_ENGINES) if resolve_binary(name)]
        checks.append(
            Check(
                "gpu_solver",
                bool(found),
                ", ".join(found)
                if found
                else "missing BitCrack/RCKangaroo — run: btc-puzzle-lab engines install",
            )
        )

    state = Path(STATE_DIR)
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        probe = state / ".doctor_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        checks.append(Check("state_writable", True, str(state)))
    except OSError as exc:
        checks.append(Check("state_writable", False, str(exc)))

    env_file = Path(CONFIG_DIR) / ".env"
    checks.append(
        Check(
            "transfer_env",
            True,
            f"present ({env_file})"
            if env_file.is_file()
            else f"missing ({env_file}) — copy config/.env.example when ready to sweep",
        )
    )
    try:
        settings = get_transfer_settings()
        errors = validate_transfer_settings(settings)
        checks.append(
            Check(
                "transfer_policy",
                not errors,
                format_transfer_policy(settings) if not errors else "; ".join(errors),
            )
        )
    except ValueError as exc:
        checks.append(Check("transfer_policy", False, str(exc)))

    try:
        notify = get_notify_settings()
        notify_errors = validate_notify_settings(notify)
        checks.append(
            Check(
                "notify_policy",
                not notify_errors,
                format_notify_policy(notify)
                if not notify_errors
                else "; ".join(notify_errors),
            )
        )
    except ValueError as exc:
        checks.append(Check("notify_policy", False, str(exc)))

    try:
        relay = get_relay_settings()
        relay_errors = validate_relay_settings(relay)
        checks.append(
            Check(
                "relay_policy",
                not relay_errors,
                format_relay_policy(relay)
                if not relay_errors
                else "; ".join(relay_errors),
            )
        )
    except ValueError as exc:
        checks.append(Check("relay_policy", False, str(exc)))

    catalog = root / "data" / "puzzles.json"
    checks.append(
        Check(
            "catalog",
            True,
            f"override {catalog}"
            if catalog.is_file()
            else "packaged practice catalog (optional: import-catalog)",
        )
    )
    return checks


def format_doctor(checks: list[Check] | None = None) -> str:
    rows = checks if checks is not None else run_doctor()
    lines = ["doctor preflight:", ""]
    fails = 0
    for item in rows:
        mark = "ok" if item.ok else "!!"
        if not item.ok and item.name in _HARD:
            fails += 1
        lines.append(f"  [{mark}] {item.name:<16} {item.detail}")
    lines.append("")
    lines.append(format_engine_status())
    lines.append("")
    if fails:
        lines.append(f"result: {fails} blocking issue(s)")
    else:
        lines.append("result: ready for experiment bootstrap")
    return "\n".join(lines)


def doctor_ok(checks: list[Check] | None = None) -> bool:
    rows = checks if checks is not None else run_doctor()
    return all(c.ok for c in rows if c.name in _HARD)
