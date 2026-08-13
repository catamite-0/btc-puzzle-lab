# BTC Puzzle Lab

Educational CLI lab for [Bitcoin Puzzle Transaction](https://privatekeys.pw/puzzles/bitcoin-puzzle-tx) workflows.

```text
catalog → search engine → state/HITS.jsonl → local audit → optional sweep transfer
```

This is a practice tool for **already-solved** catalog entries. It does **not** promise unsolved-puzzle breakthroughs, mining income, or production key custody. See [SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md).

Designing against the scheduling layer, or reusing the engine adapters elsewhere? Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Release:** `v0.5.0` — full loop (`once`) + GPU resource slotting for VPS hosts

## Quick start

Set dest and notify once, then pass a puzzle id. The lab probes the machine,
picks an engine, clones and compiles it if needed, and runs until a hit.

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e .

btc-puzzle-lab config --dest <your-btc-address> --notify https://ntfy.sh/your-topic
btc-puzzle-lab start 71
```

`start` writes `config/.env`, imports the catalog, installs the chosen solver
into `bin/`, then `watch`es that puzzle. Hits notify (no private keys) and
dry-run a sweep to dest. Live broadcast still needs
`AUTO_TRANSFER_LIVE_CONFIRM` plus `start --live`.

Restricted hunt boxes should not sweep or talk to Discord. Run this lab as an
always-on **control VPS** (unseal + notify + sweep) and point each hunt box at it:

```bash
# control VPS (open network) — keep config/relay-secret here; do not set RELAY_URL
btc-puzzle-lab relay-keygen
btc-puzzle-lab config --dest <your-btc-address> --notify https://discord.com/api/webhooks/...
btc-puzzle-lab config --new-relay-token
btc-puzzle-lab hub --host 0.0.0.0 --port 8787

# each hunt VPS — pubkey + token only (no dest, no relay-secret)
btc-puzzle-lab config --relay https://<control>:8787/hit \
  --relay-seal-pubkey <hex-from-keygen> --relay-token <same-token>
btc-puzzle-lab start 71
```

The hunt POST is ciphertext plus a bearer token. Put TLS (caddy/nginx) in front
of `hub` and firewall the port. Live broadcast still needs
`AUTO_TRANSFER_LIVE_CONFIRM` on the control VPS.

GPU experiment pod (RunPod etc.):

```bash
git clone https://github.com/catamite-0/btc-puzzle-lab.git
cd btc-puzzle-lab
bash scripts/machine-bootstrap.sh
source .venv/bin/activate
btc-puzzle-lab config --dest <your-btc-address> --notify https://ntfy.sh/your-topic
btc-puzzle-lab start 71
```

Local practice (tiny catalog, no external solver):

```bash
btc-puzzle-lab start 1 --prepare-only --no-sync --no-install
btc-puzzle-lab run 1
```

From a tagged release / wheel:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "git+https://github.com/catamite-0/btc-puzzle-lab.git@v0.5.0"
btc-puzzle-lab --version
btc-puzzle-lab list
```

Writable `state/` and `config/` resolve in this order:

1. `BTC_PUZZLE_LAB_HOME` (if set)
2. the git checkout root (editable / `pip install -e .`)
3. the current working directory (wheel / plain installs)

`btc-puzzle-lab config` writes `config/.env`. Do not commit it.

### Cursor Cloud

Repo-managed cloud config lives in `.cursor/environment.json` (Dockerfile + `scripts/cloud-install.sh`). Do not put secrets or `config/.env` into the image.

## Stable workflow

```text
# single machine
config dest+notify → start <puzzle>
        (host probe → pick engine → fetch/compile → watch until hit)
                 audit → notify → optional sweep

# split: hunt boxes search; control VPS executes
control: relay-keygen + dest/notify + hub
hunt:    config --relay https://control:8787/hit → start <puzzle>

# or the same steps manually:
host / adapt → engines install → once
import-catalog → plan → batch → status → audit → transfer
```

| Command | Role |
|---|---|
| `config` | Set dest / notify / relay (shared) |
| `start` | Pick engine for this host, install it, run until hit |
| `hub` | Control VPS: receive sealed hits, unseal, notify, sweep |
| `relay-keygen` | Seal keypair on the control VPS (pubkey only on hunt boxes) |
| `host` | Probe CPU / RAM / GPU / disk / engines → tier |
| `adapt` | Same probe + recommended next actions |
| `once` | Full loop on one resource slot (see [docs/LOOP.md](docs/LOOP.md)) |
| `watch` | Repeat `once` with hour/pass budgets |
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

Post-hit ops runbook: [docs/TRANSFER.md](docs/TRANSFER.md).

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

### External solvers (production toolchain)

Operators should not hand-wire someone else's binary path for the default CPU
solvers. Install them into the workspace:

```bash
# Debian/Ubuntu deps once (cmake only needed for rckangaroo):
sudo apt install -y git build-essential libssl-dev libgmp-dev cmake

btc-puzzle-lab engines install              # → bin/ + config/engines.env, then self-checks
btc-puzzle-lab engines install --only keyhunt
btc-puzzle-lab engines selfcheck            # re-verify installed solvers
btc-puzzle-lab engines                      # status
```

Missing compilers *and* missing dev headers are both reported up front with the
exact package line for your distro, instead of failing deep inside `make`.

| Engine | Install | Needs | Role |
|---|---|---|---|
| `keyhunt` | `engines install` (albertobsd/keyhunt) | address | CPU address / range search |
| `kangaroo` | `engines install` (JeanLucPons/Kangaroo, CPU) | compressed pubkey | Pollard kangaroo |
| `bitcrack` | `engines install` when `nvcc` present | address | GPU address brute-force |
| `rckangaroo` | `engines install` when `nvcc` present | compressed pubkey | GPU kangaroo (SOTA, fastest) |

Built artifacts land in ignored `vendor/` + `bin/`. Paths are written to
`config/engines.env` and auto-loaded. Explicit `*_PATH` env vars still override.

Upstream solvers are checked out at pinned commits so two hosts install the same
thing; override per engine with `BTC_PUZZLE_LAB_<ENGINE>_COMMIT`.

#### Self-check

`engines install` finishes by making each solver crack a practice puzzle whose
answer is already known, and fails if the key does not come back. A solver that
compiles and runs can still be useless — every engine here has at some point
searched correctly and then failed to hand the key over (wrong result filename,
unlabelled output, a GPU kernel that never loaded, each reported as "no hit").

```bash
btc-puzzle-lab engines selfcheck
btc-puzzle-lab engines selfcheck --only rckangaroo --selfcheck-timeout 60
btc-puzzle-lab engines install --no-selfcheck   # skip it (leaves engines unverified)
```

This runs real searches, so it is a local-only command — never wire it into CI.

```bash
btc-puzzle-lab engines
btc-puzzle-lab run 40 --auto
btc-puzzle-lab run 40 --engine keyhunt
# GPU (manual binary):
export BITCRACK_PATH=/path/to/cuBitCrack
btc-puzzle-lab run 40 --engine bitcrack
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
| `config/.env` | Local transfer / notify / relay config (gitignored) |
| `config/relay-secret` | Control VPS seal secret (gitignored, mode `0600`) |
| `data/puzzles.json` | Active catalog override (practice set in git; full import is local) |
| `data/puzzle-tx-export.csv` | Bundled full-catalog CSV snapshot |
| `vendor/` | Cloned upstream solver sources (`engines install`, gitignored) |
| `bin/` | Built solver binaries (`engines install`, gitignored) |
| `config/engines.env` | Auto-written solver paths (gitignored) |

## License

MIT
