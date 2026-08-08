# BTC Puzzle Lab

Practice lab for [Bitcoin Puzzle Transaction](https://privatekeys.pw/puzzles/bitcoin-puzzle-tx) workflows.

Goal of v0.2: run a small end-to-end pipeline on this host (2 CPU / 2 GiB):

```text
catalog → search engine → state/HITS.jsonl → local audit → optional sweep transfer
```

This is an educational workflow lab. It does **not** promise unsolved-puzzle breakthroughs, mining income, or production key custody.

## Practice catalog

| ID | Bits | Default engine | Notes |
|---:|-----:|---|---|
| 1 | 1 | `sequential` | Tiny sanity check |
| 5 | 5 | `sequential` | Fast full-range |
| 10 | 10 | `sequential` | Fast full-range |
| 16 | 16 | `sequential` | Full-range practical here |
| 20 | 20 | `sequential` | Full-range practical here |
| 24 | 24 | `window` | Use practice window / inject / keyhunt |
| 28 | 28 | `window` | Use practice window / inject / keyhunt |
| 32 | 32 | `window` | Use practice window / inject / keyhunt |
| 40 | 40 | `window` | Use practice window / inject / keyhunt |
| 45 | 45 | `window` | Use practice window / inject / keyhunt |
| 50 | 50 | `window` | Use practice window / inject / keyhunt |

## Setup

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e .
cp config/.env.example config/.env   # only needed for auto-transfer
```

### Cursor Cloud / Environment Builds

Repo-managed cloud config lives in `.cursor/environment.json`:

- base image: `.cursor/Dockerfile` (Python 3.12 + venv toolchain)
- install: `scripts/cloud-install.sh` (idempotent `.venv-dev` + editable install)

After merging, enable Builds for this environment in the Cloud Agents dashboard so new agents boot from a preinstalled snapshot. Do not put secrets or `config/.env` into the build.

## Commands

```bash
# list practice puzzles
python -m btc_puzzle_lab list

# verify catalog solution derives the address (no search)
python -m btc_puzzle_lab verify 20

# real sequential search for puzzle #20 → append HITS
python -m btc_puzzle_lab run 20

# host-aware auto strategy (engine/workers/coverage)
python -m btc_puzzle_lab strategy 40
python -m btc_puzzle_lab run 40 --auto

# list / manually call external solvers
python -m btc_puzzle_lab engines
python -m btc_puzzle_lab run 40 --engine rckangaroo
python -m btc_puzzle_lab run 40 --engine kangaroo
python -m btc_puzzle_lab run 20 --engine keyhunt

# parallel workers + resume support
python -m btc_puzzle_lab run 20 --workers 2 --resume

# puzzle #40 practice window (default engine)
python -m btc_puzzle_lab run 40

# optional: inject known solved key just to exercise HITS → audit
python -m btc_puzzle_lab run 40 --engine inject-known

# audit recorded hits (address re-derivation)
python -m btc_puzzle_lab audit

# audit + export JSON report (no private keys)
python -m btc_puzzle_lab audit --export state/audit_report.json

# optional on-chain balance check (mempool.space)
python -m btc_puzzle_lab audit --balance

# after configuring config/.env, sweep latest hit (default dry-run)
python -m btc_puzzle_lab transfer

# dry-run then structurally verify the local .txhex artifact
python -m btc_puzzle_lab transfer --verify-dry-run

# verify an existing dry-run file (never prints tx hex)
python -m btc_puzzle_lab verify-dry-run state/dryrun_....txhex

# local pipeline summary + recent structured events
python -m btc_puzzle_lab summary

# or transfer immediately after a hit
python -m btc_puzzle_lab run 20 --transfer
```

Private keys are written only under ignored `state/HITS.jsonl` (`0600`). CLI does not print them unless you pass `--show-key`. Duplicate puzzle/key hits are deduped.

## Search UX

| Flag | Meaning |
|---|---|
| `--workers N` | Process workers for sequential/window scans |
| `--resume` | Resume from `state/scan_<id>.json` checkpoint |
| `--coverage` | Scan via persistent chunk ledger (skips done ranges) |
| `--chunk-size N` | Coverage chunk size in keys (default: 65536) |
| `--order sequential\|random` | Chunk pick order for coverage mode |
| `--seed N` | RNG seed for `--order random` |
| `--max-chunks N` | Stop after N pending/in-progress chunks this run |
| `--no-progress` | Disable keys/s progress lines |

```bash
# chunked sequential coverage (mergeable across runs)
python -m btc_puzzle_lab run 16 --coverage --chunk-size 4096 --max-chunks 2

# reproducible random chunk order
python -m btc_puzzle_lab run 16 --coverage --order random --seed 42 --max-chunks 4

# inspect local coverage ledger(s)
python -m btc_puzzle_lab coverage
python -m btc_puzzle_lab coverage 16
```

Checkpoints, coverage ledgers, and structured events never store private keys.

## Auto-transfer safety gates

Configured via `config/.env` (see `config/.env.example`):

| Gate | Default | Meaning |
|---|---|---|
| `AUTO_TRANSFER_ENABLED` | `false` | Master switch |
| `AUTO_TRANSFER_DRY_RUN` | `true` | Sign + write local `.txhex`, do **not** broadcast |
| `AUTO_TRANSFER_DEST_ADDR` | empty | Destination must be a valid BTC address |
| `AUTO_TRANSFER_LIVE_CONFIRM` | empty | Live broadcast requires exact phrase `I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC` |
| `AUTO_TRANSFER_FEE_STRATEGY` | `normal` | `economy` / `normal` / `priority` estimate selection |
| `AUTO_TRANSFER_FEE_TARGET_BLOCKS` | `2` | Preferred confirmation target for fee estimates |
| `AUTO_TRANSFER_RBF` | `true` | Mark sweep inputs replaceable (`sequence=0xfffffffd`) |
| fee / dust caps | set | Fee rate is capped; dust / min-balance skips apply |

Dry-run artifacts land in ignored `state/dryrun_*.txhex` (`0600`). Signed hex is never printed to stdout. Address↔key mismatch aborts hard.

Supports sweeping compressed/uncompressed Legacy P2PKH and compressed Native Segwit P2WPKH, including multi-UTXO consolidate sweeps.

### External solvers

Lab does not vendor GPU/CPU solvers; it adapts installed binaries:

| Engine | Env | Needs | Role |
|---|---|---|---|
| `keyhunt` | `KEYHUNT_PATH` | address | CPU address / range search |
| `bitcrack` | `BITCRACK_PATH` | address | GPU address brute-force (`cuBitCrack`/`clBitCrack`) |
| `kangaroo` | `KANGAROO_PATH` | compressed pubkey | classic Pollard kangaroo |
| `rckangaroo` | `RCKANGAROO_PATH` | compressed pubkey | faster kangaroo (preferred when present) |

```bash
export KEYHUNT_PATH=/path/to/keyhunt
export BITCRACK_PATH=/path/to/cuBitCrack
export KANGAROO_PATH=/path/to/Kangaroo
export RCKANGAROO_PATH=/path/to/RCKangaroo
python -m btc_puzzle_lab engines
python -m btc_puzzle_lab run 40 --auto
python -m btc_puzzle_lab run 40 --engine bitcrack
python -m btc_puzzle_lab run 40 --engine rckangaroo --dp 16
```

`--auto` preference:

1. pubkey + large bits: `rckangaroo` → `kangaroo`
2. else address search: `bitcrack` → `keyhunt`
3. else local `window` / `sequential` / coverage

## Validate

```bash
python -m ruff check src tests
python -m pytest
```

GitHub Actions (`.github/workflows/ci.yml`) runs the same checks on pushes and PRs to `main`.

## Scope boundaries

- Independent from `coinsense` (no Discord / Gemini; own transfer module).
- Live broadcast is opt-in only behind explicit confirm.
- Unsolved high-bit puzzles remain unrealistic on this host class.
- Solved-puzzle balances are usually already spent; transfer dry-runs still exercise the pipeline.

## Local state

| Path | Purpose |
|---|---|
| `state/HITS.jsonl` | Hits (gitignored, mode `0600`) |
| `state/runs.jsonl` | Structured run events, no secrets (gitignored) |
| `state/scan_<id>.json` | Search resume checkpoints (gitignored) |
| `state/coverage_<id>.json` | Range coverage ledger / chunk status (gitignored) |
| `state/dryrun_*.txhex` | Dry-run signed txs (gitignored, mode `0600`) |
| `config/.env` | Local transfer config (gitignored) |
| `data/puzzles.json` | Practice catalog |

## License

MIT
