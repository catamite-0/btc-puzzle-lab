"""One command from three settings to a running search.

``auto`` exists because the pieces were all here and still took a dozen manual
steps to line up: import the catalog, probe the host, read the strategy output,
pick the matching solver, install build dependencies, build it, verify it, then
remember which resource flag and which dp value that engine wanted.

Given a puzzle id (plus, once, a payout address and an alert URL) this does the
whole sequence and hands control to the watch loop. Every stage reports before
the next begins, so a failure names the step that failed rather than surfacing as
a stack trace forty minutes into a build.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path

from btc_puzzle_lab.batch import prize_is_gone
from btc_puzzle_lab.catalog import Puzzle, get_puzzle
from btc_puzzle_lab.catalog_import import import_catalog
from btc_puzzle_lab.engines import ENGINES
from btc_puzzle_lab.loop import WatchResult, format_watch_result, run_watch
from btc_puzzle_lab.paths import STATE_DIR
from btc_puzzle_lab.recommend import EngineChoice, recommend_engine
from btc_puzzle_lab.runlog import log_event
from btc_puzzle_lab.settings import ConfigUpdate, bootstrap_config
from btc_puzzle_lab.strategy import (
    GPU_ENGINES,
    KANGAROO_ENGINES,
    SAFE_DP,
    HostProfile,
    probe_host,
)
from btc_puzzle_lab.toolchain import EnsureResult, ensure_engine, needs_compile

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
def _pinned_env(values: dict[str, str]) -> Iterator[None]:
    """Apply engine pins for the duration of the run, then put the env back."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def plan_file_for(puzzle_id: int) -> Path:
    """Per-target job board, so two auto runs never overwrite each other's plan."""
    return Path(STATE_DIR) / f"plan_{puzzle_id}.json"


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
    plan_only: bool = False,
    max_hours: float | None = None,
    max_passes: int | None = None,
    max_seconds: float | None = None,
    progress: bool = True,
    host: HostProfile | None = None,
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

    # 3. host
    profile = host or probe_host()
    result.host = profile
    record(
        "host",
        True,
        f"tier={profile.tier} cpus={profile.cpus} mem_mb={profile.mem_mb} "
        f"gpu={profile.gpu_name or profile.gpu}",
    )

    # 4. engine — decided from the target and the hardware, never from what
    #    happens to be installed (see recommend.py).
    if engine:
        if engine not in ENGINES and engine not in {"sequential", "window", "inject-known"}:
            record("engine", False, f"unknown engine: {engine}")
            return fail(f"unknown engine: {engine}")
        choice = EngineChoice(
            engine=engine,
            resource="gpu" if engine in GPU_ENGINES else "cpu",
            reason="pinned by --engine",
            needs_install=engine in ENGINES,
            dp=(
                dp
                if dp is not None
                else (SAFE_DP if engine in KANGAROO_ENGINES else None)
            ),
        )
    else:
        choice = recommend_engine(puzzle, profile, allow_cpu_fallback=allow_cpu_fallback)
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

    # 6. toolchain — fetch, build and verify exactly the engine we chose.
    if not choice.needs_install:
        record("toolchain", True, f"{choice.engine} is built in; nothing to build")
    elif plan_only:
        # --plan-only used to fall through to the build and only then report that
        # it had not started a search, which on a GPU box is minutes of nvcc to
        # answer a question about which engine would be picked.
        detail = (
            f"would build and verify {choice.engine}"
            if needs_compile(choice.engine)
            else f"{choice.engine} is installed; would verify it"
        )
        record("toolchain", True, f"{detail} (--plan-only)")
    elif not build:
        record("toolchain", True, f"build skipped (--no-build); assuming {choice.engine} is present")
    else:
        ensured = ensure_engine(
            choice.engine,
            install_deps=install_deps,
            selfcheck=selfcheck,
            selfcheck_timeout=selfcheck_timeout,
        )
        result.toolchain = ensured
        record("toolchain", ensured.ok, f"{choice.engine}: {ensured.message}")
        if not ensured.ok:
            return fail(f"{choice.engine} is not usable: {ensured.message}")

    if plan_only:
        result.ok = True
        result.message = (
            f"plan only: #{puzzle_id} would run {choice.engine} on the "
            f"{choice.resource} slot"
        )
        record("run", True, "not started (--plan-only)")
        return result

    # 7. run — pin the engine so the loop cannot re-derive a different one from
    #    inventory mid-session, and pin dp so a kangaroo table stays survivable.
    pins = {"BTC_PUZZLE_LAB_ENGINE": choice.engine}
    if choice.dp is not None:
        pins["BTC_PUZZLE_LAB_DP"] = str(choice.dp)
    if threads is not None:
        pins["BTC_PUZZLE_LAB_THREADS"] = str(threads)
    plan_path = plan_file_for(puzzle_id)
    # Hunt boxes post sealed hits to the hub; the hub sweeps. Local dest+relay
    # would sign twice if both sides are live.
    hunt_relay = bool(os.getenv("RELAY_URL", "").strip())
    record(
        "run",
        True,
        f"watch --ids {puzzle_id} --resource {choice.resource} "
        + " ".join(f"{k.rsplit('_', 1)[-1].lower()}={v}" for k, v in sorted(pins.items()))
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


def format_auto_result(result: AutoResult) -> str:
    total = len(result.stages)
    lines = [stage.format(i, total) for i, stage in enumerate(result.stages, start=1)]
    if result.watch is not None:
        lines.append("")
        lines.append(format_watch_result(result.watch))
    if result.message:
        lines.append("")
        lines.append(result.message)
    return "\n".join(lines)
