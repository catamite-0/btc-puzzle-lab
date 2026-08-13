"""Operator path: dest + notify once, then ``start <puzzle>``.

The host probe picks an engine. If that binary is missing, the toolchain
clones and builds it, then the loop runs until a hit (or ``--once``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from btc_puzzle_lab.batch import LOCAL_ENGINES, _classify_job
from btc_puzzle_lab.catalog import Puzzle, get_puzzle
from btc_puzzle_lab.catalog_import import ImportResult, import_catalog
from btc_puzzle_lab.crypto import is_valid_btc_address
from btc_puzzle_lab.doctor import doctor_ok, run_doctor
from btc_puzzle_lab.engines import resolve_binary
from btc_puzzle_lab.loop import LoopResult, WatchResult, run_once, run_watch
from btc_puzzle_lab.paths import ENV_EXAMPLE_FILE, ENV_FILE, workspace_root
from btc_puzzle_lab.settings import (
    LIVE_CONFIRM_PHRASE,
    get_notify_settings,
    get_transfer_settings,
    load_dotenv_files,
    validate_notify_settings,
    validate_transfer_settings,
)
from btc_puzzle_lab.strategy import HostProfile, StrategyPlan, plan_strategy, probe_host
from btc_puzzle_lab.toolchain import (
    INSTALLABLE,
    SELFCHECK_PUZZLES,
    InstallResult,
    format_install_results,
    format_selfcheck_results,
    install_engines,
    selfcheck_engines,
)

PUZZLE_ENV = "BTC_PUZZLE_LAB_PUZZLE"

# If the recommended solver cannot be built on this host, try the next family.
_INSTALL_FALLBACK = {
    "rckangaroo": "kangaroo",
    "bitcrack": "keyhunt",
}


@dataclass(frozen=True)
class OperatorConfig:
    dest_addr: str
    webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    puzzle_id: int | None = None
    live: bool = False

    @property
    def notify_configured(self) -> bool:
        if self.webhook_url.strip():
            return True
        return bool(self.telegram_bot_token.strip() and self.telegram_chat_id.strip())


@dataclass
class StartPrep:
    config: OperatorConfig
    env_path: Path
    host: HostProfile
    puzzle: Puzzle
    plan: StrategyPlan
    recommended_engine: str
    installed: list[InstallResult]
    selfcheck_text: str
    sync: ImportResult | None
    blocker: str | None = None


def upsert_env_file(path: Path, updates: dict[str, str]) -> None:
    """Merge KEY=value rows into ``path``, preserving comments and other keys."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing: list[str] = []
    if path.is_file():
        existing = path.read_text(encoding="utf-8").splitlines()
    elif Path(ENV_EXAMPLE_FILE).is_file():
        existing = Path(ENV_EXAMPLE_FILE).read_text(encoding="utf-8").splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key, _sep, _rest = stripped.partition("=")
        key = key.strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    pending = [k for k in updates if k not in seen]
    if pending:
        if out and out[-1].strip():
            out.append("")
        out.append("# written by btc-puzzle-lab config / start")
        for key in pending:
            out.append(f"{key}={updates[key]}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _apply_env(updates: dict[str, str]) -> None:
    for key, value in updates.items():
        os.environ[key] = value
    from dotenv import load_dotenv

    if Path(ENV_FILE).is_file():
        load_dotenv(ENV_FILE, override=True)


def load_operator_config() -> OperatorConfig:
    load_dotenv_files()
    raw_puzzle = os.getenv(PUZZLE_ENV, "").strip()
    puzzle_id = int(raw_puzzle) if raw_puzzle.isdigit() else None
    live = os.getenv("AUTO_TRANSFER_DRY_RUN", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }
    return OperatorConfig(
        dest_addr=os.getenv("AUTO_TRANSFER_DEST_ADDR", "").strip(),
        webhook_url=os.getenv("NOTIFY_WEBHOOK_URL", "").strip(),
        telegram_bot_token=os.getenv("NOTIFY_TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("NOTIFY_TELEGRAM_CHAT_ID", "").strip(),
        puzzle_id=puzzle_id,
        live=live,
    )


def merge_operator_config(
    base: OperatorConfig | None = None,
    *,
    dest_addr: str | None = None,
    webhook_url: str | None = None,
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
    puzzle_id: int | None = None,
    live: bool | None = None,
) -> OperatorConfig:
    cfg = base or load_operator_config()
    return OperatorConfig(
        dest_addr=(dest_addr if dest_addr is not None else cfg.dest_addr).strip(),
        webhook_url=(webhook_url if webhook_url is not None else cfg.webhook_url).strip(),
        telegram_bot_token=(
            telegram_bot_token if telegram_bot_token is not None else cfg.telegram_bot_token
        ).strip(),
        telegram_chat_id=(
            telegram_chat_id if telegram_chat_id is not None else cfg.telegram_chat_id
        ).strip(),
        puzzle_id=puzzle_id if puzzle_id is not None else cfg.puzzle_id,
        live=cfg.live if live is None else live,
    )


def config_field_errors(cfg: OperatorConfig) -> list[str]:
    """Format checks for whatever is already filled in (partial config is ok)."""
    errors: list[str] = []
    if cfg.dest_addr and not is_valid_btc_address(cfg.dest_addr):
        errors.append("--dest / AUTO_TRANSFER_DEST_ADDR is not a valid BTC address")
    if cfg.webhook_url:
        parsed = cfg.webhook_url.lower()
        if not (parsed.startswith("https://") or parsed.startswith("http://")):
            errors.append("--notify / NOTIFY_WEBHOOK_URL must be an http(s) URL")
    if cfg.telegram_bot_token and not cfg.telegram_chat_id:
        errors.append("--telegram-token set but --telegram-chat is empty")
    if cfg.telegram_chat_id and not cfg.telegram_bot_token:
        errors.append("--telegram-chat set but --telegram-token is empty")
    if cfg.puzzle_id is not None and cfg.puzzle_id < 1:
        errors.append("puzzle id must be >= 1")
    return errors


def operator_errors(cfg: OperatorConfig, *, require_puzzle: bool = False) -> list[str]:
    errors = config_field_errors(cfg)
    if not cfg.dest_addr:
        errors.append("set a sweep address with: btc-puzzle-lab config --dest <btc-address>")
    if not cfg.notify_configured:
        errors.append(
            "set notify with: btc-puzzle-lab config --notify <https-url> "
            "(or --telegram-token and --telegram-chat)"
        )
    if require_puzzle and cfg.puzzle_id is None:
        errors.append("pass a puzzle id: btc-puzzle-lab start 71")
    if cfg.live:
        confirm = os.getenv("AUTO_TRANSFER_LIVE_CONFIRM", "").strip()
        if confirm != LIVE_CONFIRM_PHRASE:
            errors.append(
                "live sweep requires AUTO_TRANSFER_LIVE_CONFIRM="
                f"{LIVE_CONFIRM_PHRASE} in config/.env"
            )
    return errors


def persist_operator_config(cfg: OperatorConfig) -> Path:
    """Write dest/notify (and last puzzle) into config/.env and enable those paths."""
    updates: dict[str, str] = {}
    if cfg.dest_addr:
        updates["AUTO_TRANSFER_DEST_ADDR"] = cfg.dest_addr
        updates["AUTO_TRANSFER_ENABLED"] = "true"
        updates["AUTO_TRANSFER_DRY_RUN"] = "false" if cfg.live else "true"
    if cfg.webhook_url:
        updates["NOTIFY_WEBHOOK_URL"] = cfg.webhook_url
    if cfg.telegram_bot_token:
        updates["NOTIFY_TELEGRAM_BOT_TOKEN"] = cfg.telegram_bot_token
    if cfg.telegram_chat_id:
        updates["NOTIFY_TELEGRAM_CHAT_ID"] = cfg.telegram_chat_id
    if cfg.notify_configured:
        updates["NOTIFY_ENABLED"] = "true"
    if cfg.puzzle_id is not None:
        updates[PUZZLE_ENV] = str(cfg.puzzle_id)
    target = Path(ENV_FILE)
    upsert_env_file(target, updates)
    _apply_env(updates)
    return target


def format_operator_config(cfg: OperatorConfig | None = None) -> str:
    item = cfg or load_operator_config()
    channels: list[str] = []
    if item.webhook_url:
        channels.append("webhook")
    if item.telegram_bot_token and item.telegram_chat_id:
        channels.append("telegram")
    ch = ",".join(channels) if channels else "(none)"
    dest = item.dest_addr or "(unset)"
    puzzle = f"#{item.puzzle_id}" if item.puzzle_id is not None else "(unset)"
    transfer = "live broadcast" if item.live else "dry-run on hit"
    return "\n".join(
        [
            f"dest       : {dest}",
            f"notify     : {ch}",
            f"puzzle     : {puzzle}  (last start; override with start <id>)",
            f"transfer   : {transfer}",
            f"env        : {ENV_FILE}",
        ]
    )


def missing_solver(plan: StrategyPlan) -> str | None:
    if plan.engine in LOCAL_ENGINES:
        return None
    if resolve_binary(plan.engine) is not None:
        return None
    return plan.engine


def _install_named(name: str) -> list[InstallResult]:
    if name not in INSTALLABLE:
        return [
            InstallResult(name, False, None, f"{name} is not an installable solver"),
        ]
    return install_engines([name])


def _engine_ready(name: str) -> bool:
    return name in LOCAL_ENGINES or resolve_binary(name) is not None


def _adopt_engine(plan: StrategyPlan, engine: str, reason: str) -> StrategyPlan:
    return replace(plan, engine=engine, reason=reason)


def prepare_start(
    cfg: OperatorConfig,
    *,
    sync: bool = True,
    install: bool = True,
    selfcheck: bool = True,
    host: HostProfile | None = None,
) -> StartPrep:
    """Probe host, pick an engine, clone/build it if needed."""
    errors = operator_errors(cfg, require_puzzle=True)
    if errors:
        raise ValueError("\n".join(errors))
    assert cfg.puzzle_id is not None

    env_path = persist_operator_config(cfg)
    transfer_errors = validate_transfer_settings(get_transfer_settings())
    notify_errors = validate_notify_settings(get_notify_settings())
    policy_errors = transfer_errors + notify_errors
    if policy_errors:
        raise ValueError("; ".join(policy_errors))

    profile = host or probe_host()
    sync_result: ImportResult | None = None
    if sync:
        sync_result = import_catalog()

    try:
        puzzle = get_puzzle(cfg.puzzle_id)
    except KeyError:
        if not sync:
            raise
        raise KeyError(
            f"unknown puzzle #{cfg.puzzle_id} after catalog import"
        ) from None

    plan = plan_strategy(puzzle, host=profile)
    recommended = plan.engine
    installed: list[InstallResult] = []
    check_text = ""

    wanted = missing_solver(plan)
    if wanted and not install:
        _status, blocker = _classify_job(plan, puzzle)
        return StartPrep(
            config=cfg,
            env_path=env_path,
            host=profile,
            puzzle=puzzle,
            plan=plan,
            recommended_engine=recommended,
            installed=installed,
            selfcheck_text=check_text,
            sync=sync_result,
            blocker=blocker or f"{wanted} is not installed (--no-install)",
        )

    if wanted and install:
        try:
            installed.extend(_install_named(wanted))
        except (RuntimeError, ValueError) as exc:
            return StartPrep(
                config=cfg,
                env_path=env_path,
                host=profile,
                puzzle=puzzle,
                plan=plan,
                recommended_engine=recommended,
                installed=installed,
                selfcheck_text=check_text,
                sync=sync_result,
                blocker=str(exc),
            )
        if not _engine_ready(wanted):
            alt = _INSTALL_FALLBACK.get(wanted)
            fail_msg = next(
                (item.message for item in reversed(installed) if item.name == wanted),
                f"{wanted} install failed",
            )
            if alt:
                if not _engine_ready(alt):
                    try:
                        installed.extend(_install_named(alt))
                    except (RuntimeError, ValueError) as exc:
                        return StartPrep(
                            config=cfg,
                            env_path=env_path,
                            host=profile,
                            puzzle=puzzle,
                            plan=plan,
                            recommended_engine=recommended,
                            installed=installed,
                            selfcheck_text=check_text,
                            sync=sync_result,
                            blocker=str(exc),
                        )
                if _engine_ready(alt):
                    plan = _adopt_engine(
                        plan,
                        alt,
                        reason=(
                            f"tier={plan.tier}: {wanted} unavailable ({fail_msg}); "
                            f"falling back to {alt}"
                        ),
                    )
                else:
                    _status, blocker = _classify_job(plan, puzzle)
                    return StartPrep(
                        config=cfg,
                        env_path=env_path,
                        host=profile,
                        puzzle=puzzle,
                        plan=plan,
                        recommended_engine=recommended,
                        installed=installed,
                        selfcheck_text=check_text,
                        sync=sync_result,
                        blocker=blocker or fail_msg,
                    )
            else:
                _status, blocker = _classify_job(plan, puzzle)
                return StartPrep(
                    config=cfg,
                    env_path=env_path,
                    host=profile,
                    puzzle=puzzle,
                    plan=plan,
                    recommended_engine=recommended,
                    installed=installed,
                    selfcheck_text=check_text,
                    sync=sync_result,
                    blocker=blocker or fail_msg,
                )

    if install and selfcheck:
        fresh = [
            item.name
            for item in installed
            if item.ok
            and item.name in SELFCHECK_PUZZLES
            and "already installed" not in item.message
        ]
        if fresh:
            checks = selfcheck_engines(fresh)
            check_text = format_selfcheck_results(checks)
            failed = [item for item in checks if not item.ok]
            if failed:
                return StartPrep(
                    config=cfg,
                    env_path=env_path,
                    host=profile,
                    puzzle=puzzle,
                    plan=plan,
                    recommended_engine=recommended,
                    installed=installed,
                    selfcheck_text=check_text,
                    sync=sync_result,
                    blocker=(
                        "engine self-check failed: "
                        + "; ".join(f"{item.name}: {item.message}" for item in failed)
                    ),
                )

    _status, blocker = _classify_job(plan, puzzle)
    if _status == "blocked":
        return StartPrep(
            config=cfg,
            env_path=env_path,
            host=profile,
            puzzle=puzzle,
            plan=plan,
            recommended_engine=recommended,
            installed=installed,
            selfcheck_text=check_text,
            sync=sync_result,
            blocker=blocker,
        )
    return StartPrep(
        config=cfg,
        env_path=env_path,
        host=profile,
        puzzle=puzzle,
        plan=plan,
        recommended_engine=recommended,
        installed=installed,
        selfcheck_text=check_text,
        sync=sync_result,
        blocker=None,
    )


def format_start_prep(prep: StartPrep, *, watch: bool = True) -> str:
    puzzle = prep.puzzle
    plan = prep.plan
    mode = "watch until hit" if watch else "single once pass"
    transfer = "live broadcast" if prep.config.live else "dry-run sweep on hit"
    channels: list[str] = []
    if prep.config.webhook_url:
        channels.append("webhook")
    if prep.config.telegram_bot_token and prep.config.telegram_chat_id:
        channels.append("telegram")
    lines = [
        f"workspace  : {workspace_root()}",
        f"dest       : {prep.config.dest_addr}",
        f"notify     : {','.join(channels) or '(none)'}",
        f"transfer   : {transfer}",
        f"host       : tier={prep.host.tier} cpus={prep.host.cpus} "
        f"mem_mb={prep.host.mem_mb} gpu={prep.host.gpu}"
        + (f" ({prep.host.gpu_name})" if prep.host.gpu_name else ""),
        f"puzzle     : #{puzzle.id} bits={puzzle.bits} status={puzzle.status}",
        f"method     : {plan.engine} resource={plan.resource}",
        f"reason     : {plan.reason}",
    ]
    if prep.recommended_engine != plan.engine:
        lines.append(f"recommended: {prep.recommended_engine} (not used)")
    if prep.sync is not None:
        lines.append(
            f"catalog    : {prep.sync.count} puzzles ({prep.sync.unsolved} unsolved) "
            f"from {prep.sync.source}"
        )
    if prep.installed:
        lines.append("install    :")
        for row in format_install_results(prep.installed).splitlines():
            lines.append(f"  {row}")
    elif plan.engine in LOCAL_ENGINES:
        lines.append("install    : skipped (local engine, no fetch/compile)")
    else:
        path = resolve_binary(plan.engine)
        lines.append(f"install    : using {path}")
    if prep.selfcheck_text:
        lines.append(prep.selfcheck_text)
    if prep.blocker:
        lines.append(f"blocked    : {prep.blocker}")
    else:
        lines.append(f"run        : {mode}")
    return "\n".join(lines)


def run_start(
    prep: StartPrep,
    *,
    watch: bool = True,
    require_doctor: bool = True,
    progress: bool = True,
    timeout: float | None = None,
    max_hours: float | None = None,
    max_passes: int | None = None,
) -> LoopResult | WatchResult:
    if prep.blocker:
        raise RuntimeError(prep.blocker)
    if require_doctor and not doctor_ok(run_doctor()):
        raise RuntimeError("doctor reported blocking issues; fix then retry `start`")
    common = dict(
        sync=False,
        status="all",
        bits_min=None,
        bits_max=None,
        puzzle_ids=[prep.puzzle.id],
        limit=1,
        stop_on_hit=True,
        resource=prep.plan.resource,
        require_doctor=False,
        audit=True,
        check_balance=False,
        transfer=True,
        notify=True,
        progress=progress,
        timeout=timeout,
        host=prep.host,
    )
    if watch:
        return run_watch(
            max_hours=max_hours,
            max_passes=max_passes,
            idle_sleep=30.0,
            sync_every=1,
            **common,
        )
    return run_once(**common)
