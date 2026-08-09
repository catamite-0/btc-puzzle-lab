#!/usr/bin/env bash
# One-shot bootstrap for a GPU/CPU experiment machine (e.g. RunPod A40).
# Safe defaults: no live transfer config is written.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

select_python() {
  local requested="${BTC_PUZZLE_LAB_PYTHON:-}"
  local candidate
  for candidate in "$requested" python3.12 python3; do
    [[ -n "$candidate" ]] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

if ! PYTHON_BIN="$(select_python)"; then
  echo "Python 3.12+ is required." >&2
  echo "Use an Ubuntu 24.04 / Python 3.12 image or set BTC_PUZZLE_LAB_PYTHON." >&2
  exit 2
fi
echo "==> python: $("$PYTHON_BIN" --version 2>&1) ($PYTHON_BIN)"

echo "==> apt build deps"
if [[ "$(id -u)" -eq 0 ]]; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git build-essential libssl-dev libgmp-dev python3-venv python3-dev
elif command -v sudo >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git build-essential libssl-dev libgmp-dev python3-venv python3-dev
else
  echo "root access or sudo is required to install build dependencies" >&2
  exit 2
fi

if ! "$PYTHON_BIN" -c 'import ensurepip, venv' 2>/dev/null; then
  echo "venv/ensurepip missing for $PYTHON_BIN; install its matching venv package" >&2
  exit 1
fi

echo "==> python venv + editable install"
"$PYTHON_BIN" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .

echo "==> solver toolchain"
btc-puzzle-lab engines install --force

echo "==> full catalog"
btc-puzzle-lab import-catalog

echo "==> preflight"
btc-puzzle-lab doctor
btc-puzzle-lab adapt

cat <<'EOF'

bootstrap done.

Next (full loop, one GPU slot):
  btc-puzzle-lab once --ids 71 --resource gpu

Or manual board:
  btc-puzzle-lab plan --status unsolved --bits-min 32 --verbose
  btc-puzzle-lab batch --limit 1 --stop-on-hit

Loop notes:     see docs/LOOP.md
Machine notes:  see docs/MACHINE.md
Transfer later: see docs/TRANSFER.md
EOF
