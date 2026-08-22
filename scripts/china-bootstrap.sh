#!/usr/bin/env bash
# One-shot bring-up for a rented GPU box in mainland China, aimed at a short
# validation run rather than a long search.
#
#   bash china-bootstrap.sh
#
# What it optimises for:
#   - minutes, not hours: only RCKangaroo is built, since it is the only engine
#     that matters on a 40/50-series card for a pubkey target
#   - the network reality: PyPI goes through a domestic mirror, and GitHub is
#     retried through a mirror host if the direct clone stalls
#   - proving the install rather than assuming it: finishes with the engine
#     self-check and a measured throughput figure
#
# Discord and Telegram are unreachable from there, so notifications stay off.
# For a one-day trial you are watching the terminal anyway.
set -euo pipefail

REPO="${REPO:-https://github.com/catamite-0/btc-puzzle-lab.git}"
# ghfast/ghproxy style mirrors change often; override if this one is down.
GH_MIRROR="${GH_MIRROR:-https://ghfast.top/}"
PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
WORKDIR="${WORKDIR:-/root/btc-puzzle-lab}"
TARGET="${TARGET:-140}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1/6  system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || true
apt-get install -y -qq git build-essential cmake libssl-dev libgmp-dev python3-venv \
    || echo "  apt failed — if the build later fails, install these by hand"

say "2/6  checking the GPU"
nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv,noheader
CAP="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1 | tr -d '.')"
case "$CAP" in
    89|120) echo "  compute capability $CAP — RCKangaroo ships a prebuilt kernel for this" ;;
    *) echo "  WARNING: compute capability $CAP has no prebuilt cubin (only sm_89 / sm_120)."
       echo "           RCKangaroo will load no kernel and sit at 0 MKeys/s without exiting." ;;
esac

say "3/6  fetching the lab"
if [ ! -d "$WORKDIR/.git" ]; then
    git clone --quiet "$REPO" "$WORKDIR" \
        || git clone --quiet "${GH_MIRROR}${REPO}" "$WORKDIR" \
        || { echo "  clone failed both direct and via mirror"; exit 1; }
fi
cd "$WORKDIR"

say "4/6  python environment"
# shellcheck source=scripts/lib-python.sh
source "$WORKDIR/scripts/lib-python.sh"
PY="$(require_python)" || exit 1
echo "  interpreter: $PY ($("$PY" -V))"
"$PY" -m venv .venv
./.venv/bin/python -m pip install -q --upgrade pip -i "$PIP_INDEX"
./.venv/bin/python -m pip install -q . -i "$PIP_INDEX"
./.venv/bin/btc-puzzle-lab --version

say "5/6  building RCKangaroo (the only engine that matters here)"
# Mirror the solver remote too; the toolchain reads these env vars.
if ! git ls-remote --exit-code https://github.com/RetiredC/RCKangaroo.git >/dev/null 2>&1; then
    echo "  github unreachable directly, switching the solver remote to the mirror"
    export BTC_PUZZLE_LAB_RCKANGAROO_REPO="${GH_MIRROR}https://github.com/RetiredC/RCKangaroo.git"
fi
./.venv/bin/btc-puzzle-lab engines install --only rckangaroo

say "6/6  measuring this card"
# The self-check already ran as part of install; this is the throughput number
# the whole rental decision turns on.
timeout 120 ./.venv/bin/btc-puzzle-lab run "$TARGET" --engine rckangaroo --max-seconds 90 2>&1 \
    | tr '\r' '\n' | grep -oE "Speed: [0-9]+ MKeys/s" | tail -5

cat <<'EOF'

Done. What the numbers mean:

  self-check all [ok]   the install can actually return a key, not just compile
  Speed: ~14500        expected for a 4090 (we measure 17500 on a 5090)

  Much lower than that, or 0, means the kernel did not load for this card.

To leave it running for the rest of the rental:

  export BTC_PUZZLE_LAB_DP=30      # flat DP table; the default fills RAM in hours
  # This GPU box is a hunt worker. Post sealed hits to the always-on control VPS:
  ./.venv/bin/btc-puzzle-lab auto 140 --engine rckangaroo \
      --relay https://<control-vps>:8787/hit \
      --relay-seal-pubkey <hex> \
      --relay-token <token>
EOF
