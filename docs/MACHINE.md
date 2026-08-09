# Machine experiment bootstrap

> Full closed loop: [LOOP.md](LOOP.md) (`btc-puzzle-lab once`).  
> Transfer ops: [TRANSFER.md](TRANSFER.md).

Short path after a RunPod (or similar) GPU pod is up.

## 1. Deploy a bounded benchmark Pod

| Setting | Recommendation |
|---|---|
| Product | **Pods** (not Serverless) |
| First GPU | **RTX 4090 Community on-demand**; A/B against Secure/5090 later |
| Instance | On-demand — avoid Spot for long unsolved runs |
| Image | `runpod/base:1.0.2-cuda1281-ubuntu2404@sha256:e10e75ecb02def99471f0dce2ea51712e4491080c233100cb0aa173ed15ccc52` |
| Container disk | 20–30 GiB |
| Volume disk | 20 GiB mounted on `/workspace` (checkout, venv, solvers, state) |
| Ports | SSH `22/tcp` only; Jupyter is unnecessary |
| Concurrency | **1 GPU = 1 puzzle** |

Keep the image's `/start.sh`. Do not put API keys, transfer configuration, or
private keys in the image. Set an external/provider-side stop deadline before
starting work. If the GPU is idle, stop the Pod.

This is an engineering benchmark, not an economically plausible #71 search.
The interval contains `2^70` candidates. Even at 10 GKey/s, expected time to a
hit is about 1,871 years; a six-hour run has roughly a 1-in-5.47-million chance.

## 2. Bootstrap

```bash
cd /workspace
git clone https://github.com/catamite-0/btc-puzzle-lab.git
cd btc-puzzle-lab
export BTC_PUZZLE_LAB_HOME=/workspace/btc-puzzle-lab
bash scripts/machine-bootstrap.sh
source .venv/bin/activate
```

The bootstrap refuses Python older than 3.12 instead of creating a broken
environment. Set `BTC_PUZZLE_LAB_PYTHON=/path/to/python3.12` when the desired
interpreter is not named `python3.12` or `python3`.

What it does:

- installs build deps + Python package
- `engines install --force` → pinned keyhunt / kangaroo, and **bitcrack** when `nvcc` exists
  (makefile patched with detected `COMPUTE_CAP` + dual SASS/PTX gencode)
- `import-catalog` → full puzzle list in workspace `data/puzzles.json`
- `doctor` + `adapt` preflight

## 3. Verify GPU solvers

```bash
nvidia-smi
btc-puzzle-lab doctor
btc-puzzle-lab engines
# if BitCrack missing but CUDA is present:
btc-puzzle-lab engines install --only bitcrack --force
```

Confirm compute capability shows **12.0** (reported as `120`) on 5090.

The bundled catalog is a snapshot. Before spending money on an unsolved target,
independently confirm its live status and balance; do not treat
`import-catalog` alone as a live-state check.

## 4. Correctness and five-minute benchmark

```bash
# known solved range: prove the binary returns the expected kind of result
btc-puzzle-lab run 20 --engine bitcrack --no-progress

# bounded #71 throughput sample; no transfer or notification credentials
btc-puzzle-lab once --ids 71 --resource gpu --max-seconds 300 \
  --no-transfer --no-notify --no-progress
```

Record keys/s, GPU utilization, power, and total cost, then stop the Pod. Compare
cards using the same project commit, solver commits, parameters, and time window.

BitCrack progress is persisted at `state/bitcrack_<id>.continue`. Before using
Spot, run a three-minute session, terminate it, restart the same command, and
confirm that the continue file advances rather than restarting the keyspace.

For a longer bounded session:

```bash
btc-puzzle-lab watch --ids 71 --resource gpu --max-hours 6 \
  --no-transfer --no-notify --no-progress
```

`--max-hours` stops the solver loop only; it does **not** stop or delete the
RunPod Pod. Set a provider-side termination deadline/cost guard as well, and
verify the Pod is stopped when the session ends.

Optional BitCrack tuning (env):

| Env | Purpose |
|---|---|
| `BTC_PUZZLE_LAB_GPU_INDEX` | CUDA device index (`-d`) |
| `BTC_PUZZLE_LAB_BITCRACK_BLOCKS` | `-b` |
| `BTC_PUZZLE_LAB_BITCRACK_THREADS` | `-t` |
| `BTC_PUZZLE_LAB_BITCRACK_POINTS` | `-p` |

Start without grid overrides; only tune if `nvidia-smi` shows the card under-used.

Manual board (same strategy under the hood):

```bash
btc-puzzle-lab plan --status unsolved --bits-min 32 --verbose
btc-puzzle-lab status
btc-puzzle-lab batch --limit 1 --stop-on-hit
btc-puzzle-lab strategy 71
btc-puzzle-lab run 71 --auto
```

CPU on the same box: leave it for `once` orchestration / audit / transfer.
Optional second puzzle only if it is a **different** pubkey/CPU job and does not starve the GPU search.

## 5. Transfer (only after a real hit)

Keep dry-run until verified. See [TRANSFER.md](TRANSFER.md).

```bash
install -m 600 config/.env.example config/.env
# set DEST_ADDR; leave DRY_RUN=true
btc-puzzle-lab transfer --verify-dry-run
```

`once` will call sweep automatically when transfer is enabled; defaults still skip/dry-run.

## Useful commands

| Command | Purpose |
|---|---|
| `doctor` | Blocking preflight |
| `adapt` | Host tier + next actions |
| `engines install` | Build solvers into `bin/` |
| `once` | Full sync → plan → search → audit → sweep attempt |
| `plan` / `batch` / `status` | Catalog automation board |
