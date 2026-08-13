# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- Control VPS hub: hunt boxes `auto --relay https://<control>:8787/hit`; the
  always-on host runs `hub` to unseal, notify, and sweep (`RELAY_TOKEN`,
  `relay-keygen`). Dest stays on the hub, not on hunt machines.

## [0.6.0] — 2026-08-13

Out-of-the-box single-target automation, plus Taproot payout addresses.

### Breaking
- `once` / `watch --no-audit` now still performs the sweep. The two are separate
  switches and the sweep was nested inside the audit branch, so skipping
  verification silently skipped the transfer as well. Use `--no-transfer` to
  search without sweeping
- Payout addresses on witness versions above 1 are rejected. They previously
  passed address validation and then built a malformed output script, so nothing
  that worked before stops working — the failure just happens up front now

### Added
- `btc-puzzle-lab auto <id>`: one command from a payout address, an alert URL and a
  puzzle id to a running search — host probe → engine choice → build dependencies →
  clone at a pinned commit → compile → known-answer self-check → watch loop, with
  every stage reported before the next begins (`docs/AUTO.md`)
- `recommend.py`: engine choice derived from the target and the hardware only, never
  from installed inventory. A GPU with no CUDA toolkit is reported as blocked with a
  remedy rather than silently downgraded to the CPU, since that changes expected time
  to solve by orders of magnitude
- `auto` pins `dp=30` for kangaroo-class runs: the engine default of 16 grows the DP
  table ~35 GB/h and is OOM-killed in ~3.4 h on a 116 GB host, discarding the table
- `settings.bootstrap_config()` / `write_env_values()`: persist payout and notify
  settings into `config/.env` (mode `0600`), preserving hand-written keys
- `toolchain.ensure_build_deps()` / `ensure_engine()`: install missing compilers and
  headers through apt/dnf (`sudo -n`, never an interactive prompt), then build and
  verify exactly one engine
- Per-target job boards (`state/plan_<id>.json`) so concurrent runs cannot overwrite
  each other's plan
- Hit notifications via `NOTIFY_WEBHOOK_URL` and/or Telegram (`NOTIFY_TELEGRAM_*`)
- `once` / `watch` auto-notify on hit; `--no-notify` to skip; never ships private keys

- Taproot (`bc1p…`) payout addresses, via bech32m (BIP-350). Witness v0 keeps
  bech32 and v1 requires bech32m, with a mismatch rejected rather than tolerated;
  witness versions above 1 are refused because their spending rules are undefined,
  so funds sent there would be non-standard and anyone-can-spend. Taproot is a
  destination only — spending *from* a v1 output needs Schnorr signing, which this
  lab does not implement

### Fixed
- Witness outputs above v0 were built with the raw version byte instead of
  `OP_1..OP_16`, so a Taproot destination would have produced a script that does
  not encode the intended program. Unreachable before bech32m support, fixed with it
- `parse_privkey_text` returned the first hex token on any line mentioning a
  private key, without checking it derives the address being searched. Since `add`
  is valid hex, a line like `priv add 5` could mask a genuine hit further down and
  turn a solved puzzle into an address-mismatch crash
- Multi-worker scans recorded whichever chunk finished last as the resume point.
  Because workers complete out of order, `--resume` could restart past ranges no
  worker had scanned; the checkpoint now advances only across the contiguous
  completed prefix
- `run_batch` raised a bare `KeyError` when the board outlived the catalog it was
  built from (a full import reverted to the practice subset between `plan` and
  `batch`). Such jobs are marked blocked with an explanation
- The swept-prize check ran for every runnable job before the loop started, so
  `--limit 1` still paid an explorer call per job it would never reach. It now runs
  immediately before each job executes
- `runlog` sanitisation walked dicts but not lists, so `{"hits": [{"private_key_hex": …}]}`
  reached `state/runs.jsonl` intact
- `scripts/watchdog.py` converted run-log timestamps with `mktime` minus
  `time.timezone`, which is an hour out whenever local DST is in effect
- `once` / `watch --resource auto` aborted on every large CPU-only host: tier
  `compute` was routed to the GPU queue, and that tier by definition has neither a
  card nor a GPU solver. `adapt` and `doctor` made the same misclassification
- `--no-audit` silently skipped the sweep as well, because the transfer step was
  nested inside the audit branch. Transfer is its own switch again; a failed audit
  still blocks it
- `RCKANGAROO_PATH` was missing from the in-process env map, so a run that had just
  built RCKangaroo did not export the path it produced

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
