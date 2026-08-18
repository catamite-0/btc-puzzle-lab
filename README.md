# BTC Puzzle Lab

Educational CLI lab for [Bitcoin Puzzle Transaction](https://privatekeys.pw/puzzles/bitcoin-puzzle-tx) workflows.

```text
catalog → search engine → state/HITS.jsonl → local audit → optional sweep transfer
```

This is a practice tool for **already-solved** catalog entries. It does **not** promise unsolved-puzzle breakthroughs, mining income, or production key custody. See [SECURITY.md](SECURITY.md) and [CHANGELOG.md](CHANGELOG.md).

Want it to just run? [docs/AUTO.md](docs/AUTO.md). Designing against the scheduling layer, or reusing the engine adapters elsewhere? Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Release:** `v0.7.0` — `auto` hunt + `hub` control VPS (unseal, notify, sweep)

## Quick start

Three settings and a puzzle id — everything else is derived:

```bash
git clone https://github.com/catamite-0/btc-puzzle-lab.git
cd btc-puzzle-lab
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .

btc-puzzle-lab auto 140 \
    --dest bc1qyour-payout-address \
    --notify https://ntfy.sh/your-topic
```

That probes the host, picks the engine this machine and target call for, installs
its build dependencies, clones it at a pinned commit, compiles it, proves it works
against a puzzle with a known answer, and starts hunting. Later runs need only the
id. Sweeps stay **dry-run** until you pass `--live`. Full guide:
[docs/AUTO.md](docs/AUTO.md).

```bash
btc-puzzle-lab auto 140 --plan-only   # show the engine decision, build nothing
btc-puzzle-lab auto 140               # reuses the stored dest / notify
```

Restricted hunt VPS: post sealed hits to an always-on control host that runs
`hub` (unseal + Discord + sweep). Dest stays there. See [docs/AUTO.md](docs/AUTO.md).

Manual control instead:

```bash
python -m pip install -r requirements-dev.txt
btc-puzzle-lab engines install
btc-puzzle-lab doctor
btc-puzzle-lab list
btc-puzzle-lab run 1
# GPU pod (RunPod etc.): bash scripts/machine-bootstrap.sh
# details: docs/MACHINE.md , docs/LOOP.md
```

From a tagged release / wheel:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "git+https://github.com/catamite-0/btc-puzzle-lab.git@v0.7.0"
btc-puzzle-lab --version
btc-puzzle-lab list
```

Needs **Python 3.12+**. The bootstrap scripts check this first and name an
interpreter to install if the host is older; `pip` alone reports it as a
resolver error two minutes into the run.

Writable `state/` and `config/` resolve in this order:

1. `BTC_PUZZLE_LAB_HOME` (if set)
2. the git checkout root (editable / `pip install -e .`)
3. the current working directory (wheel / plain installs)

Copy `config/.env.example` → `config/.env` only when exercising auto-transfer.
Installed from a wheel, with no checkout to copy from? `btc-puzzle-lab config
--write-example` writes that template into the workspace first.

### Cursor Cloud

Repo-managed cloud config lives in `.cursor/environment.json` (Dockerfile + `scripts/cloud-install.sh`). Do not put secrets or `config/.env` into the image.

## The workflow

There is one:

```bash
btc-puzzle-lab auto <id>
```

It runs seven stages and reports each one before starting the next, so a failure
names the step that failed instead of surfacing forty minutes into a build:

```text
config → catalog → host → engine → target → build+verify → hunt
                                                             └─ audit → sweep
```

| | |
|---|---|
| `auto <id>` | The above, end to end ([docs/AUTO.md](docs/AUTO.md)) |
| `auto <id> --plan-only` | Print the engine decision and stop. Builds nothing, takes ~0.2s |
| `config --dest … --notify …` | Store payout and alert once; later runs need only the id |
| `hub` | Control VPS: receive sealed hits, unseal, notify, sweep |

Sweeps stay dry-run until you pass `--live`.

<details>
<summary><b>The steps underneath, as separate commands</b></summary>

`auto` drives these for you. Reach for them when you want to see what it saw, or
when it stopped and you want to redo one step by hand. `--help-all` lists them.

| Command | Role |
|---|---|
| `adapt` (alias `host`) | Probe CPU / RAM / GPU / disk / engines → tier, and what to do about it |
| `doctor` | Blocking preflight |
| `engines install` / `selfcheck` | Build solvers into `bin/`; prove they return a key |
| `import-catalog` | Load the full catalog into workspace `data/puzzles.json` |
| `plan` | Build the catalog-wide job board (`state/batch_plan.json`) |
| `batch` | Execute ready jobs (limit / resume / stop-on-hit) |
| `status` | Matrix: job status × coverage × hit |
| `once` | One pass of sync → plan → slot → audit → sweep ([docs/LOOP.md](docs/LOOP.md)) |
| `watch` | Repeat `once` with hour / pass budgets |
| `run <id>` | A single search, bypassing the job board |
| `strategy <id>` | What `run` would choose from **installed** solvers — not the `auto` decision |
| `list` / `verify` / `coverage` / `summary` | Catalog and local state |
| `audit` / `transfer` / `verify-dry-run` | The money path after a hit |
| `relay-keygen` / `unseal` / `relay-flush` | Control-VPS relay ops |

```bash
btc-puzzle-lab adapt
btc-puzzle-lab plan --status unsolved --bits-min 32 --verbose
btc-puzzle-lab status
btc-puzzle-lab batch --limit 5 --stop-on-hit
```

</details>

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

Sweeps **from** compressed/uncompressed Legacy P2PKH and compressed Native Segwit P2WPKH, including multi-UTXO consolidate sweeps.

Payout destinations (`AUTO_TRANSFER_DEST_ADDR`) may be Legacy P2PKH (`1…`), P2SH (`3…`), Native Segwit P2WPKH/P2WSH (`bc1q…`) or **Taproot P2TR (`bc1p…`, bech32m)**. Witness versions above 1 are refused on purpose: they have no defined spending rules yet, so funds sent there are non-standard to relay and anyone-can-spend by consensus.

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

`bin/` is per-workspace, but the upstream checkouts and their object files are
shared across workspaces on the same host, so a second clone of this repo copies
the binaries instead of recompiling them:

1. `BTC_PUZZLE_LAB_CACHE` (if set) → `<cache>/vendor/`
2. an existing workspace `vendor/` (hosts provisioned before this behaviour)
3. `~/.cache/btc-puzzle-lab/vendor/`

A build already present in that tree is reused as-is. `engines install --force`
recompiles regardless — needed after changing a `*_COMMIT` pin, or to rebuild
BitCrack for a different card.

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

`auto` verifies the engine on every run, but not by re-searching every time: a
pass is recorded in `state/selfcheck.json` against the SHA-256 of the binary that
produced it, and a matching digest is accepted without re-running. A rebuild, a
different commit or a swapped binary changes the digest and earns a fresh check.
`engines selfcheck` always searches for real and refreshes that record.

For the GPU engines the record is scoped to the detected compute capability too,
so moving a workspace to a different card re-verifies rather than trusting a
kernel that may not load on it.

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
| `vendor/` | Cloned upstream solver sources + build trees (shared cache; see above) |
| `state/selfcheck.json` | Which solver builds have passed the self-check here |
| `bin/` | Built solver binaries (`engines install`, gitignored) |
| `config/engines.env` | Auto-written solver paths (gitignored) |

## License

MIT
