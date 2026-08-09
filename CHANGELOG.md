# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- Hit notifications via `NOTIFY_WEBHOOK_URL` and/or Telegram (`NOTIFY_TELEGRAM_*`)
- `benchmark-gpu`: two bounded BitCrack rounds against a fresh random hash
  target, with checkpoint-resume, log-redaction, and HITS-integrity validation

### Changed
- `once` / `watch` now default to no catalog sync, solved practice entries,
  transfer off, and notifications off; each formerly automatic action is opt-in
- Runpod guidance is random-synthetic-only and contains no funded or unsolved target
- Search execution is restricted to entries whose included solved-practice key
  verifies against the catalog address

### Security
- GitHub workflow regression tests block solver, Runpod, GPU, catalog-search,
  and self-hosted-runner commands

## [0.5.0] — 2026-08-09

Full-loop orchestrator for VPS hosts (CPU/GPU resource slots).

### Added
- `btc-puzzle-lab once`: sync unsolved → plan → exclusive resource slot → audit → optional sweep
- `btc-puzzle-lab watch`: repeat `once` with `--max-hours` / `--max-passes` budgets
- Job `resource` tag (`cpu` / `gpu`) from strategy engine class
- `docs/LOOP.md` and RTX 5090-oriented `docs/MACHINE.md` guidance
- BitCrack makefile dual SASS/PTX gencode helper (`build_gencode`) for arches like `sm_120`
- Doctor advisory check when a GPU host is missing BitCrack
- Streaming redacted external-solver logs + optional timeout (`--max-seconds`)
- BitCrack device/grid env knobs (`BTC_PUZZLE_LAB_GPU_INDEX`, `_BITCRACK_BLOCKS/THREADS/POINTS`)

### Changed
- GPU VPS default remains one puzzle per card (`once --limit 1`)
- `adapt` recommendations point at `once` for GPU hosts

## [0.4.1] — 2026-08-09

Machine-ready polish for experiment pods.

### Added
- `engines install` builds **BitCrack** (`cuBitCrack`) when `nvcc` is present
  (auto `CUDA_HOME` / `COMPUTE_CAP` makefile patch)
- `btc-puzzle-lab doctor` preflight
- `scripts/machine-bootstrap.sh` + `docs/MACHINE.md` one-shot pod setup
- Transfer landing (from 0.4.0 unreleased work): confirmed-only UTXOs,
  fee-from-signed-vsize, `MAX_FEE_SATS`, enriched dry-run verify,
  broadcast failover/status, `transfer --broadcast-dry-run`, `docs/TRANSFER.md`

### Changed
- Default `engines install` set: keyhunt + kangaroo (+ bitcrack if CUDA)

## [0.4.0] — 2026-08-08

Production pivot: first-class solver toolchain. Operators install upstream CPU
solvers through the lab instead of wiring someone else's `KEYHUNT_PATH` by hand.

### Added
- `btc-puzzle-lab engines install` clones/builds upstream `keyhunt` + `kangaroo`
  into workspace `bin/` and writes `config/engines.env` (auto-loaded)
- Modern-g++ patch for JeanLucPons/Kangaroo `Timer.h` (`cstdint`)
- Docs for Debian build deps (`git build-essential libssl-dev libgmp-dev`)

### Changed
- `engines` CLI gains `status` / `install` subcommands; default remains status
- Binary resolve uses env + workspace `bin/` only (respects `BTC_PUZZLE_LAB_HOME`)
- `adapt` / batch blockers point at `engines install`
- BitCrack / RCKangaroo remain manual (CUDA / upstream packaging)

## [0.3.0] — 2026-08-08

Stable cut of the post-0.2 automation surface: full catalog import, catalog-wide
batch board, and environment-adaptive host tiers.

### Added
- `import-catalog` to load the full 160-puzzle Bitcoin Puzzle Transaction list
  from the bundled CSV snapshot (or `--url` / `--from-csv`)
- Catalog automation board: `plan` → `batch` → `status` (`state/batch_plan.json`)
- Environment-adaptive host tiers (`host` / `adapt`): CPU/RAM/GPU probe, knobs,
  and env overrides (`BTC_PUZZLE_LAB_CPUS` / `_MEM_MB` / `_GPU`)

### Fixed
- Address-puzzle algorithm fallback now prefers `keyhunt` on standard/constrained
  hosts and `bitcrack` on gpu/compute (was a no-op ternary)
- `--auto --dp` no longer treats the CLI default as an explicit override
- `plan --plan` accepted as an alias of `--output`
- Unknown-puzzle errors refer to the active catalog (not only the practice set)
- `status` missing-plan errors go to stderr (consistent with `batch`)

### Changed
- Packaged User-Agent follows `__version__`
- Docs and release workflow aligned for tagged `v0.3.0` ships

## [0.2.0] — 2026-08-08

### Added
- Practice catalog for solved puzzles `#1,5,10,16,20,24,28,32,40,45,50`
- Coverage ledger with chunked / random scans (`coverage` CLI)
- Host-aware `--auto` strategy and external engine adapters
  (`keyhunt`, `bitcrack`, `kangaroo`, `rckangaroo`)
- Gated auto-transfer sweep (disabled + dry-run by default)
- HITS audit export, dry-run verify, runlog / summary
- GitHub Actions CI (ruff + pytest)
- Cursor Cloud bootstrap (`.cursor/environment.json`, Dockerfile, install script)
- Packaged catalog + env template for wheel / non-editable installs

### Security
- Private keys only in local `state/` (gitignored, mode `0600`)
- Live broadcast requires exact confirm phrase
- CLI prints keys only with `--show-key`

## [0.1.0] — 2026-08-07

### Added
- Initial practice lab: catalog → search → HITS → audit
