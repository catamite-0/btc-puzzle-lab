#!/usr/bin/env bash
# One-shot bootstrap for a GPU/CPU experiment machine (e.g. RunPod A40).
# Safe defaults: no live transfer config is written.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> apt build deps (if sudo available)"
if command -v sudo >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git build-essential libssl-dev libgmp-dev python3-venv python3-dev
else
  echo "    sudo missing — ensure git/make/g++/libssl-dev/libgmp-dev are installed"
fi

echo "==> python venv + editable install"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .

echo "==> solver toolchain"
btc-puzzle-lab engines install || true

echo "==> full catalog (optional algorithm board)"
btc-puzzle-lab import-catalog || true

echo "==> preflight"
btc-puzzle-lab doctor
btc-puzzle-lab adapt

cat <<'EOF'

bootstrap done.

Next:
  btc-puzzle-lab plan --status unsolved --bits-min 32 --verbose
  btc-puzzle-lab status
  btc-puzzle-lab batch --limit 3 --stop-on-hit

Transfer (later): see docs/TRANSFER.md
Machine notes:     see docs/MACHINE.md
EOF
