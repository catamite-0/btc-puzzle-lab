#!/usr/bin/env bash
# Idempotent Cloud Agent / Environment Build install.
# Install only — no long-running services, no live transfer config.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! python3 -c "import venv, ensurepip" 2>/dev/null; then
  if command -v sudo >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq python3-venv python3-dev build-essential libffi-dev libssl-dev
  else
    echo "python3-venv missing and sudo unavailable" >&2
    exit 1
  fi
fi

python3 -m venv .venv-dev
.venv-dev/bin/python -m pip install -U pip
.venv-dev/bin/python -m pip install -r requirements-dev.txt
.venv-dev/bin/python -m pip install -e .
.venv-dev/bin/python -c "import btc_puzzle_lab; print('btc_puzzle_lab', btc_puzzle_lab.__version__)"
