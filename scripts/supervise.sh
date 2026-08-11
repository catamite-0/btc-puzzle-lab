#!/bin/sh
# Restart the watchdog if it ever dies. Nothing else supervises it, and this
# container has no init system to lean on (PID 1 is docker-init, systemd is
# offline), so the outer loop is deliberately the dumbest thing that works.
#
#   setsid nohup ./scripts/supervise.sh > logs/supervise.log 2>&1 < /dev/null &
#
# BTC_PUZZLE_LAB_BIN should point at a frozen install so that editing the
# working tree cannot change what a relaunch runs.
set -u

WORKSPACE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE" || exit 1

RUN_PY="${WATCHDOG_PYTHON:-$WORKSPACE/.venv-run/bin/python}"
[ -x "$RUN_PY" ] || RUN_PY="$WORKSPACE/.venv-dev/bin/python"
export BTC_PUZZLE_LAB_BIN="${BTC_PUZZLE_LAB_BIN:-$WORKSPACE/.venv-run/bin/btc-puzzle-lab}"

while true; do
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') supervise: starting watchdog ($RUN_PY)"
    "$RUN_PY" "$WORKSPACE/scripts/watchdog.py" >> "$WORKSPACE/logs/watchdog.log" 2>&1
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') supervise: watchdog exited ($?), restarting in 10s"
    sleep 10
done
