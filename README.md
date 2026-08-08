# BTC Puzzle Lab

Educational CLI lab for [Bitcoin Puzzle Transaction](https://privatekeys.pw/puzzles/bitcoin-puzzle-tx) workflows.

```text
catalog → search engine → state/HITS.jsonl → local audit → optional sweep transfer
```

This is a practice tool for **already-solved** catalog entries. It does **not** promise unsolved-puzzle breakthroughs, mining income, or production key custody. See [SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md).

**Stable release:** `v0.3.0`

## Quick start

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e .
btc-puzzle-lab list
btc-puzzle-lab verify 1
btc-puzzle-lab run 1
```

From a tagged release / wheel:

```bash
python3 -m venv .venv
source .venv/activate
python -m pip install "git+https://github.com/catamitez0-maker/btc-puzzle-lab.git@v0.3.0"
btc-puzzle-lab --version
btc-puzzle-lab list
```

Writable `state/` and `config/` resolve in this order:

1. `BTC_PUZZLE_LAB_HOME` (if set)
2. the git checkout root (editable / `pip install -e .`)
3. the current working directory (wheel / plain installs)

Copy `config/.env.example` → `config/.env` only when exercising auto-transfer.

### Cursor Cloud

Repo-managed cloud config lives in `.cursor/environment.json` (Dockerfile + `scripts/cloud-install.sh`). Do not put secrets or `config/.env` into the image.

## Stable workflow

```text
host / adapt → import-catalog → plan → batch → status
                      ↓
              strategy / run --auto   (single puzzle, same adaptive rules)
                      ↓
             HITS → audit → (optional) transfer
```

| Command | Role |
|---|---|
| `host` | Probe CPU / RAM / GPU / disk / engines → tier |
| `adapt` | Same probe + recommended next actions |
| `import-catalog` | Load full catalog into workspace `data/puzzles.json` |
| `plan` | Build catalog-wide job board (`state/batch_plan.json`) |
| `batch` | Execute ready jobs (limit / resume / stop-on-hit) |
| `status` | Matrix: job status × coverage × hit |
| `run --auto` | Single-puzzle path (same adaptive strategy) |

```bash
btc-puzzle-lab host
btc-puzzle-lab adapt
btc-puzzle-lab import-catalog
btc-puzzle-lab plan --status unsolved --bits-min 32 --verbose
btc-puzzle-lab status
btc-puzzle-lab batch --limit 5 --stop-on-hit
```

### Environment adaptation

Host is classified into a tier that drives workers / threads / chunk / window / dp:

| Tier | When | Effect |
|---|---|---|
| `constrained` | low RAM/CPU | small chunks, 1 worker |
| `standard` | ~2+ GiB, 2+ CPU | balanced local + external |
| `gpu` | NVIDIA detected or GPU solvers installed | prefer BitCrack / RCKangaroo |
| `compute` | high CPU/RAM | larger chunks/windows/threads |

Overrides (container / CI friendly):

```bash
export BTC_PUZZLE_LAB_CPUS=4
export BTC_PUZZLE_LAB_MEM_MB=8192
export BTC_PUZZLE_LAB_GPU=1   # or 0 to force off
btc-puzzle-lab adapt
```

Blocked jobs are intentional: preferred algorithm is recorded even when the solver binary is missing (`BITCRACK_PATH` / `RCKANGAROO_PATH` / …).

## Catalog

Default install ships a small **practice** catalog (solved puzzles for pipeline drills).

Import the **full** Bitcoin Puzzle Transaction list (160 entries). Default uses the
bundled CSV snapshot (`data/puzzle-tx-export.csv`); this writes workspace
`data/puzzles.json` (overrides the packaged practice set for local runs — do not
commit a full import unless you intend to):

```bash
btc-puzzle-lab import-catalog
btc-puzzle-lab import-catalog --from-csv /path/to/export.csv
btc-puzzle-lab import-catalog --no-solutions
# optional live refresh (may fail behind Cloudflare)
btc-puzzle-lab import-catalog --url \
  'https://privatekeys.pw/puzzles/bitcoin-puzzle-tx/export?status=all'

btc-puzzle-lab list
btc-puzzle-lab strategy 71
```

Unsolved rows have `practice_solution_hex: null`; kangaroo-class engines need
`pubkey_compressed_hex` when the export includes it.

### Practice subset (shipped default)

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

## Commands

```bash
btc-puzzle-lab list
btc-puzzle-lab verify 20
btc-puzzle-lab run 20
btc-puzzle-lab strategy 40
btc-puzzle-lab run 40 --auto
btc-puzzle-lab engines
btc-puzzle-lab run 40 --engine rckangaroo
btc-puzzle-lab run 40 --engine kangaroo
btc-puzzle-lab run 20 --engine keyhunt
btc-puzzle-lab run 20 --workers 2 --resume
btc-puzzle-lab run 40
btc-puzzle-lab run 40 --engine inject-known
btc-puzzle-lab audit
btc-puzzle-lab audit --export state/audit_report.json
btc-puzzle-lab audit --balance
btc-puzzle-lab transfer
btc-puzzle-lab transfer --verify-dry-run
btc-puzzle-lab verify-dry-run state/dryrun_....txhex
btc-puzzle-lab summary
btc-puzzle-lab run 20 --transfer
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
btc-puzzle-lab run 16 --coverage --chunk-size 4096 --max-chunks 2
btc-puzzle-lab run 16 --coverage --order random --seed 42 --max-chunks 4
btc-puzzle-lab coverage
btc-puzzle-lab coverage 16
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
btc-puzzle-lab engines
btc-puzzle-lab run 40 --auto
btc-puzzle-lab run 40 --engine bitcrack
btc-puzzle-lab run 40 --engine rckangaroo --dp 16
```

`--auto` preference:

1. pubkey + large bits: `rckangaroo` → `kangaroo`
2. else address search: `bitcrack` → `keyhunt`
3. else local `window` / `sequential` / coverage

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | No hit / audit failure / transfer error |
| 2 | Bad args / unknown puzzle / config error |
| 3 | Transfer skipped by safety gates (disabled / policy) |

## Validate

```bash
python -m ruff check src tests
python -m pytest
```

GitHub Actions (`.github/workflows/ci.yml`) runs the same checks on pushes and PRs to `main`. Tagging `v*` runs `.github/workflows/release.yml` (build wheel, smoke install, GitHub Release).

## Scope boundaries

- Standalone lab (no Discord / Gemini; own transfer module).
- Live broadcast is opt-in only behind explicit confirm.
- Unsolved high-bit puzzles remain unrealistic on a 2 CPU / 2 GiB host.
- Solved-puzzle balances are usually already spent; transfer dry-runs still exercise the pipeline.

## Local state

| Path | Purpose |
|---|---|
| `state/HITS.jsonl` | Hits (gitignored, mode `0600`) |
| `state/runs.jsonl` | Structured run events, no secrets (gitignored) |
| `state/scan_<id>.json` | Search resume checkpoints (gitignored) |
| `state/coverage_<id>.json` | Range coverage ledger / chunk status (gitignored) |
| `state/batch_plan.json` | Catalog automation board (gitignored via `state/`) |
| `state/dryrun_*.txhex` | Dry-run signed txs (gitignored, mode `0600`) |
| `config/.env` | Local transfer config (gitignored) |
| `data/puzzles.json` | Active catalog override (practice set in git; full import is local) |
| `data/puzzle-tx-export.csv` | Bundled full-catalog CSV snapshot |

## License

MIT
