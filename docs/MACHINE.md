# Machine experiment bootstrap

> This page covers the standalone lab/solver board. To join the public btcpuzzle.info #71 pool on RTX 5090, use [RUNPOD_POOL.md](RUNPOD_POOL.md); `btc-puzzle-lab run 71 --auto` does not join that pool.

Short path after a RunPod (or similar) GPU pod is up.

## 1. Deploy pod

- Product: **Pods** (not Serverless)
- Card: **A40** (or RTX 4090 if Community pricing is better)
- Template: Ubuntu + CUDA, enable SSH or Jupyter

## 2. Bootstrap

```bash
git clone https://github.com/catamitez0-maker/btc-puzzle-lab.git
cd btc-puzzle-lab
bash scripts/machine-bootstrap.sh
source .venv/bin/activate
```

What it does:

- installs build deps + Python package
- `engines install` → keyhunt / kangaroo, and **bitcrack** when `nvcc` exists
- `import-catalog` → full puzzle list in workspace `data/puzzles.json`
- `doctor` + `adapt` preflight

## 3. Verify GPU solvers

```bash
nvidia-smi
btc-puzzle-lab engines
# if BitCrack missing but CUDA is present:
btc-puzzle-lab engines install --only bitcrack --force
export BITCRACK_PATH="$PWD/bin/cuBitCrack"   # usually auto via config/engines.env
```

## 4. Run board

```bash
btc-puzzle-lab plan --status unsolved --bits-min 32 --verbose
btc-puzzle-lab status
btc-puzzle-lab batch --limit 3 --stop-on-hit
# or single puzzle:
btc-puzzle-lab strategy 71
btc-puzzle-lab run 71 --auto
```

## 5. Transfer (only after a real hit)

Keep dry-run until verified. See [TRANSFER.md](TRANSFER.md).

```bash
cp config/.env.example config/.env
# set DEST_ADDR; leave DRY_RUN=true
btc-puzzle-lab transfer --verify-dry-run
```

## Useful commands

| Command | Purpose |
|---|---|
| `doctor` | Blocking preflight |
| `adapt` | Host tier + next actions |
| `engines install` | Build solvers into `bin/` |
| `plan` / `batch` / `status` | Catalog automation board |
