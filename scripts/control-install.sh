#!/usr/bin/env bash
# Install the control VPS: the always-on box that receives sealed hits, unseals
# them, notifies, and sweeps.
#
#   bash scripts/control-install.sh
#
# Deliberately not machine-bootstrap.sh. That script installs a compiler and
# builds keyhunt/kangaroo, which a control host never runs — and building
# cryptocurrency solvers is the sort of thing free-tier providers write ToS
# clauses about. The hub needs the Python package and nothing else: no gcc, no
# libgmp, no CUDA. Verified on a 1 GB shared-core instance.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/lib-python.sh
source "$ROOT/scripts/lib-python.sh"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "1/4  python"
PY="$(require_python)" || exit 1
echo "  $PY ($("$PY" -V))"

say "2/4  package (no build toolchain)"
"$PY" -m venv .venv
./.venv/bin/python -m pip install -q --upgrade pip
./.venv/bin/python -m pip install -q .
./.venv/bin/btc-puzzle-lab --version

say "3/4  seal keypair"
SECRET="$ROOT/config/relay-secret"
if [ -f "$SECRET" ]; then
    echo "  $SECRET already exists — keeping it (delete it to rotate)"
else
    ./.venv/bin/btc-puzzle-lab relay-keygen
fi

say "4/4  what is left for you"
cat <<'EOF'

  1. BACK UP config/relay-secret OFFLINE, NOW.
     It is the only unrecoverable thing here. Lose it and every sealed hit a
     hunt box has already queued becomes permanently undecryptable.

  2. Set the payout address and an alert channel, and mint a hunt token:

       ./.venv/bin/btc-puzzle-lab config \
           --dest bc1q... \
           --notify https://ntfy.sh/your-topic \
           --new-relay-token

     The token prints once. Sweeps stay dry-run until you deliberately enable
     live broadcast — see docs/TRANSFER.md before you do.

  3. Put the hub behind TLS and start it. It speaks plain HTTP and holds the
     key that unseals private keys, so it must not be exposed directly:

       docs/DEPLOY.md

EOF
