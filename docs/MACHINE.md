# Runpod synthetic GPU benchmark

This runbook measures BitCrack throughput and restart behavior without selecting
a catalog or user-supplied Bitcoin address. Each command creates a fresh CSPRNG
hash target and a non-network Base58 input string accepted by the pinned solver.
No private scalar is generated; the target has no known key or funds, is hidden
from logs, and must never be funded.

## 1. Create a bounded Pod

| Setting | Recommendation |
|---|---|
| Product | **Pods** (not Serverless) |
| First GPU | **RTX 4090 Community on-demand** |
| Capacity | On-demand for a reproducible short sample |
| Image | `runpod/base:1.0.2-cuda1281-ubuntu2404@sha256:e10e75ecb02def99471f0dce2ea51712e4491080c233100cb0aa173ed15ccc52` |
| Container disk | 20–30 GiB |
| Volume disk | 20 GiB mounted on `/workspace` |
| Ports | SSH `22/tcp` only |
| Concurrency | One benchmark process per GPU |

Set a provider-side termination deadline before starting. A 45–60 minute guard
normally leaves enough time to clone, compile, run two rounds (three minutes
total), inspect the result, and stop the Pod. The process timeout does
not stop or delete the Pod.

Do not add Runpod API keys, transfer configuration, wallet material, catalog
targets, or notification credentials to the image or Pod environment.

## 2. Bootstrap

```bash
cd /workspace
git clone https://github.com/catamite-0/btc-puzzle-lab.git
cd btc-puzzle-lab
export BTC_PUZZLE_LAB_HOME=/workspace/btc-puzzle-lab
bash scripts/machine-bootstrap.sh
source .venv/bin/activate
```

The bootstrap:

- refuses Python older than 3.12;
- installs the package and required build dependencies;
- builds only the pinned BitCrack source required by this GPU benchmark;
- runs `doctor` and `adapt`;
- does **not** import the full catalog or start a search.

## 3. Verify the toolchain

```bash
nvidia-smi
btc-puzzle-lab doctor
btc-puzzle-lab engines
```

If CUDA is present but BitCrack is missing:

```bash
btc-puzzle-lab engines install --only bitcrack --force
```

## 4. Verify the package, then run only the synthetic GPU check

First, run a non-search verification of a bundled, already-solved fixture:

```bash
btc-puzzle-lab verify 20
```

Then run the synthetic throughput/resume check:

```bash
btc-puzzle-lab benchmark-gpu --seconds 90
```

The benchmark has no address, keyspace, or puzzle-ID options. Each invocation
creates a fresh random hash target and a fresh high-numbered checkpoint; its two
bounded rounds share that private in-process target, and the second must resume
past the first cursor. The wrapper redacts the target from its command display and
streamed solver output. The underlying hash has no known private key or funds, but
it must never be funded.
`--seconds` is hard-limited to 75–90 per round because the pinned BitCrack
checkpoint interval is 60 seconds.

A passing result reports:

- a short fingerprint of the ephemeral target, never the target itself;
- checkpoint-derived MKey/s for each round;
- checkpoint elapsed time advancing by at least 50 seconds per round;
- a strictly increasing restart cursor;
- no creation or modification of `state/HITS.jsonl`.

The cursor is stored at a fresh `state/bitcrack_9xxxxxxxx.continue` path. A later
benchmark invocation deliberately allocates a different path and target.

## 5. Compare and tear down

For card comparisons, keep the project commit, pinned solver commit, container
image, grid parameters, and 90-second window identical. Record the displayed
MKey/s, GPU utilization, power, and provider cost.

Optional BitCrack tuning variables:

| Env | Purpose |
|---|---|
| `BTC_PUZZLE_LAB_GPU_INDEX` | CUDA device index (`-d`) |
| `BTC_PUZZLE_LAB_BITCRACK_BLOCKS` | `-b` |
| `BTC_PUZZLE_LAB_BITCRACK_THREADS` | `-t` |
| `BTC_PUZZLE_LAB_BITCRACK_POINTS` | `-p` |

After collecting the bounded result, stop and delete the Pod and verify the
Runpod account has no test Pod or unintended network volume left behind.

See [LOOP.md](LOOP.md) for the solved-practice defaults and [SECURITY.md](../SECURITY.md)
for the acceptable-use boundary.
