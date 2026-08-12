#!/usr/bin/env python3
"""Keep long solver runs honest: restart what dies, warn before it dies.

`watch` already relaunches a solver that exits, which is exactly why a sick run
can look healthy. On this host RCKangaroo's DP table grew ~35 GB/hour against a
116 GB container limit, so the OOM killer took it every ~3.5 hours and every
restart discarded all accumulated work. GPU utilisation stayed at 99% throughout
and nothing was ever reported.

So this watches three things the loop itself cannot:

1. the `watch` process is alive at all (nothing else restarts *that*)
2. solver memory, projected against the cgroup limit, before the kill happens
3. how often solvers are restarting, which catches churn from any other cause

Alerts reuse the lab's own notify channel. Run it detached:

    setsid nohup ./scripts/watchdog.py > logs/watchdog.log 2>&1 < /dev/null &
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1]
os.environ.setdefault("BTC_PUZZLE_LAB_HOME", str(WORKSPACE))
load_dotenv(WORKSPACE / "config" / ".env", override=False)

from btc_puzzle_lab.notify import send_webhook  # noqa: E402
from btc_puzzle_lab.settings import get_notify_settings  # noqa: E402

# Long runs should not be launched from an editable checkout: editing src/ would
# change what the next relaunch executes, so a broken commit stops the search.
# Point this at a frozen install (`pip install .` into its own venv) and the
# working tree stays free for development.
CLI_BIN = Path(
    os.environ.get("BTC_PUZZLE_LAB_BIN", str(WORKSPACE / ".venv-dev" / "bin" / "btc-puzzle-lab"))
)

CHECK_INTERVAL = float(os.environ.get("WATCHDOG_INTERVAL", "300"))
# Warn while there is still time to act, not once the kill is imminent.
PROJECTED_OOM_WARN_MINUTES = float(os.environ.get("WATCHDOG_OOM_WARN_MINUTES", "90"))
MEMORY_WARN_FRACTION = float(os.environ.get("WATCHDOG_MEM_WARN_FRACTION", "0.80"))
RESTARTS_WARN_24H = int(os.environ.get("WATCHDOG_RESTARTS_WARN", "3"))
SPEED_ALERT_FRACTION = float(os.environ.get("WATCHDOG_SPEED_FRACTION", "0.7"))
CHURN_ONGOING_WINDOW = float(os.environ.get("WATCHDOG_CHURN_WINDOW", "3600"))
# Progress output is what makes throughput observable, so it has to be kept -
# bounded instead of silenced.
LOG_MAX_BYTES = int(os.environ.get("WATCHDOG_LOG_MAX_BYTES", str(64 * 1024 * 1024)))
ALERT_COOLDOWN = float(os.environ.get("WATCHDOG_ALERT_COOLDOWN", "3600"))

CGROUP_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
CGROUP_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_USAGE_V2 = Path("/sys/fs/cgroup/memory.current")
CGROUP_LIMIT_V2 = Path("/sys/fs/cgroup/memory.max")


@dataclass
class Job:
    """One `watch` loop this watchdog is responsible for."""

    name: str
    puzzle_id: int
    resource: str
    plan_file: str
    log_file: str
    env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)

    @property
    def pattern(self) -> str:
        return f"watch --ids {self.puzzle_id} --resource {self.resource}"

    @property
    def restart_budget_24h(self) -> int:
        """Restarts this job is *supposed* to do, plus slack.

        A job with --max-seconds recycles on purpose. Alerting on those would fire
        every day and teach the operator to ignore the channel.
        """
        if "--max-seconds" in self.extra_args:
            index = self.extra_args.index("--max-seconds")
            try:
                budget = float(self.extra_args[index + 1])
            except (IndexError, ValueError):
                return RESTARTS_WARN_24H
            if budget > 0:
                return int(86400 / budget * 1.5) + RESTARTS_WARN_24H
        return RESTARTS_WARN_24H

    def command(self) -> list[str]:
        return [
            str(CLI_BIN),
            "watch",
            "--ids",
            str(self.puzzle_id),
            "--resource",
            self.resource,
            "--no-sync",
            "--plan-file",
            self.plan_file,
            *self.extra_args,
        ]


JOBS = [
    Job(
        # #135 was retired on 2026-08-11: its 13.5 BTC was swept on 2026-07-28, so
        # the bundled catalog snapshot still showing it unsolved was simply stale.
        # #140 is the smallest pubkey target whose prize is still on chain.
        name="p140-gpu",
        puzzle_id=140,
        resource="gpu",
        plan_file="state/plan_140.json",
        log_file="logs/p140-gpu.log",
        # dp=30 keeps the DP table flat; dp=16 filled the container in ~3.4h.
        env={"BTC_PUZZLE_LAB_DP": "30"},
    ),
    Job(
        name="p160-cpu",
        puzzle_id=160,
        resource="cpu",
        plan_file="state/plan_160.json",
        log_file="logs/p160-cpu.log",
        env={"BTC_PUZZLE_LAB_THREADS": "20", "BTC_PUZZLE_LAB_ENGINE": "kangaroo"},
        extra_args=["--max-seconds", "3600"],
    ),
]


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}", flush=True)


# Cooldowns survive a watchdog restart: supervise.sh can respawn this process,
# and in-memory state would let one condition re-notify on every respawn.
_ALERT_STATE = WORKSPACE / "state" / "watchdog_alerts.json"


def _load_alert_state() -> dict[str, float]:
    try:
        return json.loads(_ALERT_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_alert_state(state: dict[str, float]) -> None:
    try:
        _ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        _ALERT_STATE.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log(f"  could not persist alert state: {exc}")


def alert(key: str, text: str, *, force: bool = False) -> None:
    """Notify, but do not repeat the same condition every cycle."""
    now = time.time()
    state = _load_alert_state()
    if not force and now - state.get(key, -1e9) < ALERT_COOLDOWN:
        log(f"[alert suppressed:{key}] {text}")
        return
    state[key] = now
    _save_alert_state(state)
    log(f"[ALERT:{key}] {text}")
    try:
        settings = get_notify_settings()
    except ValueError as exc:
        log(f"  notify settings unreadable: {exc}")
        return
    if not (settings.enabled and settings.webhook_url):
        log("  notify disabled; alert not sent")
        return
    result = send_webhook(f"⚠️ btc-puzzle-lab watchdog\n{text}", url=settings.webhook_url)
    log(f"  notify -> {result.ok} {result.message}")


def pids_matching(pattern: str) -> list[int]:
    proc = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False)
    return [int(p) for p in proc.stdout.split() if p.isdigit()]


def solver_pids(job: Job) -> list[int]:
    """Solver children of this job's watch process."""
    found: list[int] = []
    for watcher in pids_matching(job.pattern):
        proc = subprocess.run(
            ["pgrep", "-P", str(watcher)], capture_output=True, text=True, check=False
        )
        found += [int(p) for p in proc.stdout.split() if p.isdigit()]
    return found


def rss_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def cgroup_memory() -> tuple[int, int] | None:
    for usage_path, limit_path in ((CGROUP_USAGE, CGROUP_LIMIT), (CGROUP_USAGE_V2, CGROUP_LIMIT_V2)):
        try:
            raw_limit = limit_path.read_text(encoding="utf-8").strip()
            if raw_limit == "max":
                return None
            limit = int(raw_limit)
            usage = int(usage_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        # Cgroup v1 reports a sentinel when unlimited.
        if limit <= 0 or limit > (1 << 62):
            return None
        return usage, limit
    return None


def restart_history(puzzle_id: int) -> tuple[int, float | None]:
    """(restarts in 24h, seconds since the most recent one)."""
    runs = WORKSPACE / "state" / "runs.jsonl"
    if not runs.is_file():
        return 0, None
    cutoff = time.time() - 86400
    count = 0
    latest: float | None = None
    try:
        for line in runs.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or '"search_start"' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("puzzle_id") != puzzle_id or event.get("event") != "search_start":
                continue
            stamp = event.get("ts", "")
            try:
                epoch = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
            except ValueError:
                continue
            if epoch >= cutoff:
                count += 1
            if latest is None or epoch > latest:
                latest = epoch
    except OSError:
        return 0, None
    return count, (time.time() - latest if latest is not None else None)


def launch(job: Job) -> None:
    env = dict(os.environ)
    env["BTC_PUZZLE_LAB_HOME"] = str(WORKSPACE)
    env.update(job.env)
    log_path = WORKSPACE / job.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        subprocess.Popen(
            job.command(),
            cwd=WORKSPACE,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    log(f"  relaunched {job.name}")


# pid -> (timestamp, rss) of the previous observation
_rss_history: dict[int, tuple[float, int]] = {}
# job -> best sustained throughput seen, in MKeys/s
_speed_baseline: dict[str, float] = {}


def check_memory(job: Job) -> None:
    memory = cgroup_memory()
    if memory is None:
        return
    usage, limit = memory
    if usage / limit >= MEMORY_WARN_FRACTION:
        alert(
            "container-memory",
            f"container memory at {usage / 2**30:.0f}/{limit / 2**30:.0f} GB "
            f"({100 * usage / limit:.0f}%)",
        )

    now = time.monotonic()
    for pid in solver_pids(job):
        rss = rss_bytes(pid)
        if rss is None:
            continue
        previous = _rss_history.get(pid)
        _rss_history[pid] = (now, rss)
        if previous is None:
            continue
        elapsed = now - previous[0]
        grew = rss - previous[1]
        if elapsed < 60 or grew <= 0:
            continue
        rate = grew / elapsed  # bytes/sec
        headroom = limit - usage
        minutes_left = headroom / rate / 60
        if minutes_left <= PROJECTED_OOM_WARN_MINUTES:
            alert(
                f"oom-projection-{job.name}",
                f"{job.name}: solver pid {pid} at {rss / 2**30:.1f} GB and growing "
                f"{rate * 3600 / 2**30:.1f} GB/h — projected to hit the "
                f"{limit / 2**30:.0f} GB container limit in ~{minutes_left:.0f} min. "
                "Raise BTC_PUZZLE_LAB_DP; an OOM kill discards all accumulated work.",
            )


_SPEED_RE = re.compile(r"(?:Speed:\s*)?([0-9]+(?:\.[0-9]+)?)\s*(M|G)(?:Key|Keys|K)/s", re.I)


def latest_speed_mkeys(job: Job) -> float | None:
    """Most recent throughput the solver reported, in MKeys/s."""
    log_path = WORKSPACE / job.log_file
    try:
        with open(log_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    best = None
    for line in tail.replace("\r", "\n").splitlines():
        if "GPU 0.00" in line:  # Kangaroo prints a zero GPU column on CPU builds
            line = line.replace("GPU 0.00 MK/s", "")
        match = _SPEED_RE.search(line)
        if match:
            value = float(match.group(1))
            best = value * 1000 if match.group(2).upper() == "G" else value
    return best


def check_throughput(job: Job) -> None:
    speed = latest_speed_mkeys(job)
    if speed is None:
        return
    baseline = _speed_baseline.get(job.name)
    if baseline is None or speed > baseline:
        # Track the best sustained rate seen; a healthy job returns to it.
        _speed_baseline[job.name] = speed
        log(f"  {job.name}: throughput {speed:,.0f} MKeys/s (baseline)")
        return
    log(f"  {job.name}: throughput {speed:,.0f} MKeys/s (baseline {baseline:,.0f})")
    if speed < baseline * SPEED_ALERT_FRACTION:
        alert(
            f"slow-{job.name}",
            f"{job.name}: throughput {speed:,.0f} MKeys/s is only "
            f"{100 * speed / baseline:.0f}% of the {baseline:,.0f} MKeys/s baseline. "
            "A second solver sharing the same GPU halves it — check for stray processes.",
        )


def rotate_log(job: Job) -> None:
    """Keep the tail; progress output is unbounded over weeks."""
    path = WORKSPACE / job.log_file
    try:
        if path.stat().st_size <= LOG_MAX_BYTES:
            return
        with open(path, "rb") as handle:
            handle.seek(-LOG_MAX_BYTES // 4, os.SEEK_END)
            tail = handle.read()
        path.write_bytes(b"[btc-puzzle-lab watchdog] log truncated\n" + tail)
        log(f"  rotated {job.log_file}")
    except OSError as exc:
        log(f"  could not rotate {job.log_file}: {exc}")


def check_job(job: Job) -> None:
    watchers = pids_matching(job.pattern)
    if not watchers:
        alert(f"down-{job.name}", f"{job.name}: watch loop was not running — restarting", force=True)
        launch(job)
        return

    churn, since_last = restart_history(job.puzzle_id)
    budget = job.restart_budget_24h
    # A 24h count keeps firing for a full day after the cause is fixed. Only raise
    # it while the churn is still happening; history alone is not an incident.
    ongoing = since_last is not None and since_last < CHURN_ONGOING_WINDOW
    if churn > budget and ongoing:
        alert(
            f"churn-{job.name}",
            f"{job.name}: solver restarted {churn} times in 24h (expected at most {budget}), "
            f"most recently {since_last / 60:.0f} min ago. Kangaroo-class engines lose their "
            "distinguished-point table on every restart, so the search may be making no "
            "cumulative progress.",
        )
    check_memory(job)
    check_throughput(job)
    rotate_log(job)


def main() -> int:
    log(f"watchdog started; interval={CHECK_INTERVAL:.0f}s jobs={[j.name for j in JOBS]}")
    memory = cgroup_memory()
    if memory:
        log(f"  container limit: {memory[1] / 2**30:.0f} GB")
    else:
        log("  no cgroup memory limit detected; OOM projection disabled")
    while True:
        for job in JOBS:
            try:
                check_job(job)
            except Exception as exc:  # noqa: BLE001 — a watchdog must not die
                log(f"  check failed for {job.name}: {exc!r}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
