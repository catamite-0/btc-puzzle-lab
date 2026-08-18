# Shared by the bootstrap scripts: find an interpreter this package installs into.
#
# pyproject sets requires-python = ">=3.12", and the GPU images people rent are
# routinely still on 3.10. Without this the failure lands two minutes into a
# bring-up as a pip resolver message ("requires a different Python") that reads
# like a packaging bug rather than "install a newer python".
#
# Source it, then: PY="$(require_python)" || exit 1

MIN_PYTHON_MINOR=12

find_python() {
    local candidate resolved
    for candidate in "${PYTHON:-}" python3 python3.14 python3.13 python3.12; do
        [ -n "$candidate" ] || continue
        resolved="$(command -v "$candidate" 2>/dev/null)" || continue
        if "$resolved" -c "import sys; raise SystemExit(sys.version_info[:2] < (3, $MIN_PYTHON_MINOR))" 2>/dev/null; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done
    return 1
}

require_python() {
    local found
    if found="$(find_python)"; then
        printf '%s\n' "$found"
        return 0
    fi
    cat >&2 <<EOF

btc-puzzle-lab needs Python 3.${MIN_PYTHON_MINOR} or newer.
  found: $(python3 -V 2>&1 || echo 'no python3 on PATH')

  Ubuntu 22.04 and older ship 3.10; install a newer one:
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update
    sudo apt-get install -y python3.12 python3.12-venv python3.12-dev

  Then re-run, or point this script at an interpreter directly:
    PYTHON=/usr/bin/python3.12 bash $0

EOF
    return 1
}
