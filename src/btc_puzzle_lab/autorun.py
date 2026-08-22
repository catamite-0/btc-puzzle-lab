"""Configure, prepare, and run one catalog target through the watch loop."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from btc_puzzle_lab.autopilot.facts import HostCapabilities
from btc_puzzle_lab.autopilot.host import HostDiscoveryError, discover_host
from btc_puzzle_lab.autopilot.planning import PlanningPolicy
from btc_puzzle_lab.batch import prize_is_gone
from btc_puzzle_lab.catalog import Puzzle, get_puzzle
from btc_puzzle_lab.catalog_import import import_catalog
from btc_puzzle_lab.engines import resolve_binary
from btc_puzzle_lab.loop import WatchResult, run_watch
from btc_puzzle_lab.paths import STATE_DIR
from btc_puzzle_lab.recommend import (
    EngineChoice,
    recommend_engine,
    recommend_pinned_engine,
)
from btc_puzzle_lab.runlog import log_event
from btc_puzzle_lab.settings import ConfigUpdate, bootstrap_config
from btc_puzzle_lab.strategy import HostProfile, classify_tier
from btc_puzzle_lab.toolchain import (
    ENGINE_ENV_VARS,
    EnsureResult,
    cuda_available,
    ensure_engine,
)

STAGES = ("config", "catalog", "host", "engine", "target", "toolchain", "run")


@dataclass(frozen=True)
class Stage:
    name: str
    ok: bool
    detail: str

    def format(self, index: int, total: int) -> str:
        mark = "ok" if self.ok else "!!"
        return f"[{index}/{total}] {self.name:<9} [{mark}] {self.detail}"


@dataclass
class AutoResult:
    puzzle_id: int
    stages: list[Stage] = field(default_factory=list)
    config: ConfigUpdate | None = None
    host: HostProfile | None = None
    choice: EngineChoice | None = None
    toolchain: EnsureResult | None = None
    watch: WatchResult | None = None
    ok: bool = False
    message: str = ""

    @property
    def failed_stage(self) -> str | None:
        return next((s.name for s in self.stages if not s.ok), None)


StageSink = Callable[[Stage], None]


@contextmanager
def _pinned_env(values: dict[str, str | None]) -> Iterator[None]:
    """Apply or clear engine knobs temporarily, then restore the environment."""
    previous = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def plan_file_for(puzzle_id: int) -> Path:
    """Per-target job board, so two auto runs never overwrite each other's plan."""
    return Path(STATE_DIR) / f"plan_{puzzle_id}.json"


def _host_failure_detail(exc: HostDiscoveryError | None) -> str:
    if exc is None:
        return "exact host discovery returned an invalid value"
    return f"exact host discovery failed ({exc.code.value})"


def _execution_profile(capabilities: HostCapabilities) -> HostProfile:
    """Project exact planner facts into the legacy loop's host value."""

    mem_mb = max(1, capabilities.memory_bytes // 2**20)
    gpu = bool(capabilities.gpus)
    usable_cpus = max(1, capabilities.cpu_count - PlanningPolicy().cpu_reserved_cores)
    return HostProfile(
        cpus=usable_cpus,
        mem_mb=mem_mb,
        engines=frozenset(),
        gpu=gpu,
        gpu_name=", ".join(device.name for device in capabilities.gpus),
        disk_free_mb=(
            None if capabilities.disk_free_bytes is None else capabilities.disk_free_bytes // 2**20
        ),
        tier=classify_tier(cpus=usable_cpus, mem_mb=mem_mb, gpu=gpu),
    )


def _execution_pins(choice: EngineChoice) -> dict[str, str | None]:
    """Bind GPU identity and remove range-changing ambient engine modes."""

    pins: dict[str, str | None] = {
        "BTC_PUZZLE_LAB_BITCRACK_RANDOM": None,
        "BTC_PUZZLE_LAB_BITCRACK_CHUNK": None,
        "BTC_PUZZLE_LAB_RCKANGAROO_START": None,
        "BTC_PUZZLE_LAB_RCKANGAROO_RANGE_BITS": None,
    }
    if choice.device_id is not None:
        pins["CUDA_VISIBLE_DEVICES"] = choice.device_id
        pins["BTC_PUZZLE_LAB_GPU_INDEX"] = "0"
    return pins


def run_auto(
    puzzle_id: int,
    *,
    dest_addr: str | None = None,
    notify_url: str | None = None,
    telegram_token: str | None = None,
    telegram_chat: str | None = None,
    live: bool = False,
    relay_url: str | None = None,
    relay_seal_pubkey: str | None = None,
    relay_token: str | None = None,
    sync: bool = True,
    engine: str | None = None,
    allow_cpu_fallback: bool = False,
    ignore_swept: bool = False,
    build: bool = True,
    install_deps: bool = True,
    selfcheck: bool = True,
    selfcheck_timeout: float = 180.0,
    dp: int | None = None,
    threads: int | None = None,
    max_hours: float | None = None,
    max_passes: int | None = None,
    max_seconds: float | None = None,
    progress: bool = True,
    on_stage: StageSink | None = None,
) -> AutoResult:
    """Configure, provision and launch an unattended search for one puzzle."""
    result = AutoResult(puzzle_id=puzzle_id)

    def record(name: str, ok: bool, detail: str) -> Stage:
        stage = Stage(name=name, ok=ok, detail=detail)
        result.stages.append(stage)
        if on_stage is not None:
            on_stage(stage)
        return stage

    def fail(message: str) -> AutoResult:
        result.ok = False
        result.message = message
        log_event("auto_failed", puzzle_id=puzzle_id, stage=result.failed_stage, detail=message)
        return result

    # 1. config — validate and persist payout / alert settings before anything slow.
    try:
        update = bootstrap_config(
            dest_addr=dest_addr,
            notify_url=notify_url,
            telegram_token=telegram_token,
            telegram_chat=telegram_chat,
            live=live,
            relay_url=relay_url,
            relay_seal_pubkey=relay_seal_pubkey,
            relay_token=relay_token,
        )
    except ValueError as exc:
        record("config", False, str(exc))
        return fail(f"config rejected: {exc}")
    result.config = update
    record("config", True, update.format())

    # 2. catalog — the full list, so ids outside the practice subset resolve.
    if sync:
        imported = import_catalog()
        detail = (
            f"{imported.count} puzzles ({imported.unsolved} unsolved, "
            f"{imported.with_pubkey} with pubkey) from {imported.source}"
        )
    else:
        detail = "using the existing workspace catalog (--no-sync)"
    try:
        puzzle: Puzzle = get_puzzle(puzzle_id)
    except KeyError as exc:
        record("catalog", False, f"{detail}; puzzle #{puzzle_id} not found")
        return fail(str(exc.args[0] if exc.args else exc))
    record(
        "catalog",
        True,
        f"{detail}; #{puzzle.id} bits={puzzle.bits} status={puzzle.status} "
        f"pubkey={'yes' if puzzle.pubkey_compressed_hex else 'no'}",
    )

    # 3. host — one exact discovery feeds both planning and the legacy loop.
    try:
        exact_host = discover_host()
    except HostDiscoveryError as exc:
        detail = _host_failure_detail(exc)
        record("host", False, detail)
        return fail(detail)
    if type(exact_host) is not HostCapabilities:
        detail = _host_failure_detail(None)
        record("host", False, detail)
        return fail(detail)
    profile = _execution_profile(exact_host)
    result.host = profile
    gpu_names = ", ".join(device.name for device in exact_host.gpus) or "none"
    record(
        "host",
        True,
        f"tier={profile.tier} cpus={exact_host.cpu_count} usable_cpus={profile.cpus} "
        f"mem_mb={profile.mem_mb} gpus={gpu_names}",
    )

    # 4. engine — decided from the target and the hardware, never from what
    #    happens to be installed (see recommend.py).
    #
    # Explicit arguments outrank compatible environment pins.
    pin_source = "--engine"
    if engine is None:
        env_engine = os.environ.get("BTC_PUZZLE_LAB_ENGINE", "").strip().lower()
        if env_engine:
            engine, pin_source = env_engine, "BTC_PUZZLE_LAB_ENGINE"
    try:
        if dp is None:
            dp = _env_int("BTC_PUZZLE_LAB_DP")
        if threads is None:
            threads = _env_int("BTC_PUZZLE_LAB_THREADS")
    except ValueError as exc:
        record("engine", False, str(exc))
        return fail(str(exc))
    if dp is not None and not 14 <= dp <= 32:
        detail = "dp must be between 14 and 32"
        record("engine", False, detail)
        return fail(detail)
    if threads is not None and threads < 1:
        detail = "threads must be greater than zero"
        record("engine", False, detail)
        return fail(detail)

    pinned = engine is not None
    if engine:
        try:
            choice = recommend_pinned_engine(
                puzzle,
                engine,
                capabilities=exact_host,
                pin_source=pin_source,
            )
        except ValueError:
            record("engine", False, f"unknown engine: {engine}")
            return fail(f"unknown engine: {engine}")
    else:
        choice = recommend_engine(puzzle, exact_host)
    if dp is not None and choice.engine not in {"kangaroo", "rckangaroo"}:
        detail = "dp applies only to kangaroo and rckangaroo"
        record("engine", False, detail)
        return fail(detail)
    if threads is not None and choice.engine in {"keyhunt", "kangaroo"} and threads > profile.cpus:
        detail = f"threads exceeds the {profile.cpus} CPU core(s) available after reservation"
        record("engine", False, detail)
        return fail(detail)
    if dp is not None:
        choice = replace(choice, dp=dp)
    result.choice = choice
    stage = record("engine", choice.ok, choice.format())
    if not stage.ok:
        return fail(choice.blocked or "no engine available for this target")

    # 5. target — a prize that has already been swept is not worth a GPU-hour,
    #    whatever the catalog snapshot says. Checked here rather than inside the
    #    loop, where a skipped job would spin instead of stopping.
    if ignore_swept:
        record("target", True, "prize check skipped (--ignore-swept)")
    elif puzzle.practice_solution is not None:
        # Practice entries were swept years ago; drilling the pipeline against them
        # is the point, so a zero balance there is expected rather than a blocker.
        record("target", True, f"practice target — #{puzzle.id} is already solved publicly")
    elif prize_is_gone(puzzle):
        record("target", False, f"{puzzle.address} has a zero balance — prize already swept")
        return fail(
            f"puzzle #{puzzle_id} has already been claimed (chain balance is 0). "
            "Pick another target, or pass --ignore-swept to search it anyway."
        )
    else:
        record("target", True, f"{puzzle.address} still holds its prize")

    # 6. toolchain — inventory and CUDA are preparation facts, not inputs to
    #    algorithm selection. A missing GPU binary needs a usable CUDA compiler;
    #    an installed binary can still be verified without one.
    existing = resolve_binary(choice.engine) if choice.needs_install else None
    if choice.manual_provisioning and existing is None:
        detail = (
            f"{choice.engine} requires manual provisioning; install and verify its binary "
            "before pinning it in auto"
        )
        record("toolchain", False, detail)
        return fail(detail)
    if choice.needs_install and not build and existing is None:
        detail = f"--no-build requested but {choice.engine} is not installed"
        record("toolchain", False, detail)
        return fail(detail)

    used_cpu_fallback = False
    if choice.resource == "gpu" and existing is None:
        if not cuda_available():
            if allow_cpu_fallback and not pinned:
                choice = recommend_engine(puzzle, exact_host, cpu_only=True)
                result.choice = choice
                if not choice.ok:
                    record("toolchain", False, choice.format())
                    return fail(choice.blocked or "no CPU fallback is available")
                if threads is not None and threads > profile.cpus:
                    detail = (
                        f"threads exceeds the {profile.cpus} CPU core(s) "
                        "available after reservation"
                    )
                    record("toolchain", False, detail)
                    return fail(detail)
                used_cpu_fallback = True
            else:
                remedy = (
                    "remove the engine pin and use --allow-cpu-fallback, or pin a CPU engine"
                    if pinned
                    else "install CUDA or use --allow-cpu-fallback"
                )
                detail = f"{choice.engine} is not installed and nvcc is unavailable; {remedy}"
                record("toolchain", False, detail)
                return fail(detail)

    if choice.resource == "gpu":
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
        if visible not in {"", "0"}:
            detail = (
                "the current toolchain cannot attest a nonzero or UUID "
                "CUDA_VISIBLE_DEVICES selector; expose only physical GPU 0"
            )
            record("toolchain", False, detail)
            return fail(detail)
        if len(exact_host.gpus) != 1:
            detail = (
                "GPU preparation requires exactly one visible device; set "
                "CUDA_VISIBLE_DEVICES=0 or choose the CPU fallback"
            )
            record("toolchain", False, detail)
            return fail(detail)

    if not choice.needs_install:
        record("toolchain", True, f"{choice.engine} is built in; nothing to build")
    else:
        with _pinned_env(_execution_pins(choice)):
            ensured = ensure_engine(
                choice.engine,
                allow_build=build and not choice.manual_provisioning,
                install_deps=install_deps,
                selfcheck=selfcheck,
                selfcheck_timeout=selfcheck_timeout,
            )
        result.toolchain = ensured
        fallback = " (CPU fallback)" if used_cpu_fallback else ""
        record("toolchain", ensured.ok, f"{choice.engine}{fallback}: {ensured.message}")
        if not ensured.ok:
            return fail(f"{choice.engine} is not usable: {ensured.message}")

    # 7. run — pin the engine so the loop cannot re-derive a different one from
    #    inventory mid-session, and pin dp so a kangaroo table stays survivable.
    pins = _execution_pins(choice)
    pins["BTC_PUZZLE_LAB_ENGINE"] = choice.engine
    if choice.dp is not None:
        pins["BTC_PUZZLE_LAB_DP"] = str(choice.dp)
    if threads is not None and choice.engine in {"keyhunt", "kangaroo"}:
        pins["BTC_PUZZLE_LAB_THREADS"] = str(threads)
    if result.toolchain is not None and result.toolchain.binary is not None:
        pins[ENGINE_ENV_VARS[choice.engine]] = str(result.toolchain.binary)
    plan_path = plan_file_for(puzzle_id)
    # Hunt boxes post sealed hits to the hub; the hub sweeps. Local dest+relay
    # would sign twice if both sides are live.
    hunt_relay = bool(os.getenv("RELAY_URL", "").strip())
    record(
        "run",
        True,
        f"watch --ids {puzzle_id} --resource {choice.resource} "
        + " ".join(
            f"{key.rsplit('_', 1)[-1].lower()}={value}"
            for key, value in sorted(pins.items())
            if value is not None
        )
        + f" plan={plan_path}"
        + (" transfer=hub" if hunt_relay else ""),
    )
    log_event(
        "auto_start",
        puzzle_id=puzzle_id,
        engine=choice.engine,
        resource=choice.resource,
        dp=choice.dp,
        tier=profile.tier,
    )

    with _pinned_env(pins):
        watch = run_watch(
            puzzle_ids=[puzzle_id],
            resource=choice.resource,
            # An explicitly named id must not be filtered out by the board's
            # default unsolved/bits-min screen.
            status="all",
            bits_min=None,
            bits_max=None,
            limit=1,
            sync=False,
            stop_on_hit=True,
            # `auto` ran its own targeted preflight above; doctor's build-tool gate
            # would otherwise block a pure-Python run on a host with no compiler.
            require_doctor=False,
            audit=True,
            transfer=not hunt_relay,
            notify=True,
            progress=progress,
            max_hours=max_hours,
            max_passes=max_passes,
            timeout=max_seconds,
            plan_path=plan_path,
            host=profile,
        )
    result.watch = watch
    result.ok = watch.last is None or watch.last.ok
    result.message = (
        f"stopped after {watch.passes} pass(es): {watch.stopped_reason}; hits={watch.hits}"
    )
    log_event(
        "auto_complete",
        puzzle_id=puzzle_id,
        engine=choice.engine,
        passes=watch.passes,
        hits=watch.hits,
        reason=watch.stopped_reason,
    )
    return result
