#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly VENV_DIR="${PROJECT_ROOT}/.venv"

die() {
    printf 'bootstrap error: %s\n' "$1" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

[[ -r /etc/os-release ]] || die "cannot verify Ubuntu release"
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || \
    die "this bootstrap requires Ubuntu 24.04"

require_command nvidia-smi
nvidia-smi -L >/dev/null || die "nvidia-smi cannot enumerate a GPU"

require_command nvcc
readonly NVCC_VERSION="$(nvcc --version)"
[[ "${NVCC_VERSION}" =~ release[[:space:]]+12\.8([,[:space:]]|$) ]] || \
    die "this bootstrap requires the CUDA 12.8 toolchain"

if (( EUID == 0 )); then
    APT=(apt-get)
else
    require_command sudo
    APT=(sudo apt-get)
fi
readonly -a APT

export DEBIAN_FRONTEND=noninteractive
"${APT[@]}" update
"${APT[@]}" install -y --no-install-recommends \
    ca-certificates \
    git \
    build-essential \
    libcurl4-openssl-dev \
    libssl-dev \
    python3-venv

require_command python3
if [[ ! -f "${PROJECT_ROOT}/pyproject.toml" && \
      ! -f "${PROJECT_ROOT}/setup.py" && \
      ! -f "${PROJECT_ROOT}/setup.cfg" ]]; then
    die "no installable Python project found at ${PROJECT_ROOT}"
fi

cd -- "${PROJECT_ROOT}"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --editable "${PROJECT_ROOT}"

export BTC_PUZZLE_LAB_HOME="${PROJECT_ROOT}"
readonly POOL_CLI="${VENV_DIR}/bin/btc-puzzle-pool"
[[ -x "${POOL_CLI}" ]] || die "editable install did not create btc-puzzle-pool"

"${POOL_CLI}" install

# This is a read-only local preflight. It does not request or run a pool range.
"${POOL_CLI}" doctor --puzzle 38

printf 'Bootstrap complete. Activate with: source %s/bin/activate\n' "${VENV_DIR}"
