# Machine experiment bootstrap

> Full closed loop: [LOOP.md](LOOP.md) (`btc-puzzle-lab once`).  
> Transfer ops: [TRANSFER.md](TRANSFER.md).

Short path after a RunPod (or similar) GPU pod is up.

## 1. Deploy pod (RTX 5090 strategy)

| Setting | Recommendation |
|---|---|
| Product | **Pods** (not Serverless) |
| GPU | **RTX 5090** (or A40 if cheaper for the same experiment) |
| Instance | On-demand — avoid Spot for long unsolved runs |
| Image | Ubuntu + **CUDA 12.8+** (needed for Blackwell / `sm_120`) |
| Disk | ≥ 20 GiB persistent volume on `/workspace` |
| Concurrency | **1 GPU = 1 puzzle** |

Billing rule: if the GPU is idle, stop the pod.

## 2. Bootstrap

```bash
git clone https://github.com/catamite-0/btc-puzzle-lab.git
cd btc-puzzle-lab
bash scripts/machine-bootstrap.sh
source .venv/bin/activate
```

What it does:

- installs build deps + Python package
- `engines install` → keyhunt / kangaroo, and **bitcrack** when `nvcc` exists
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

## 4. Run the loop (recommended)

```bash
# exclusive GPU slot
btc-puzzle-lab auto 140 --dest bc1q… --notify https://ntfy.sh/your-topic

# typical rental: this pod is a hunt box; dest/notify/sweep live on the control VPS
# btc-puzzle-lab auto 140 --relay https://<control>:8787/hit \
#   --relay-seal-pubkey <hex> --relay-token <token>

# optional budgeted session (auto-stops at wall clock):
# btc-puzzle-lab watch --ids 71 --resource gpu --max-hours 6
```

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
cp config/.env.example config/.env
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
