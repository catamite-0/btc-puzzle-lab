# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- The Release workflow can be run manually from the Actions tab, typing the
  version instead of pushing a tag. Some environments can push branches but not
  tag refs, which left releases unreachable from them. The typed version is not
  a convenience: it restores the third party to the agreement a tag push gets for
  free, so both paths still assert tag/input, `pyproject.toml` and `__version__`
  agree. A manual run also refuses to release from a non-default branch or to
  re-use a tag that already exists.
- `docs/DEPLOY.md` gains a section on enabling live broadcast: the two settings
  that must both hold, the checks that still stand between a hit and a spend,
  verifying the destination against a signed transaction rather than against the
  config (nothing can tell you `AUTO_TRANSFER_DEST_ADDR` is *yours* — only that
  it is well-formed), and the three arrangements for who actually sends.

## [0.8.0] — 2026-08-19

Bring-up stops redoing itself, and `auto` becomes the visible path.

### Added
- `docs/DEPLOY.md` and `scripts/control-install.sh` for the control VPS. The
  split between an always-on control host and disposable hunt boxes was designed
  into `auto --relay` but never written down: there was no install path that
  skipped the compiler, no TLS guidance beyond one line printed at startup, no
  service unit, and nothing telling operators to back up `config/relay-secret` —
  the one unrecoverable artifact in the whole deployment.
- `config --write-example` writes the annotated `config/.env.example` template.
  A wheel install has no checkout, so the file `doctor` and the docs point at did
  not exist there; the template ships as package data for exactly this. Refuses
  to clobber an edited file without `--force`.
- A test pins `config/.env.example` and the packaged `data/env.example`
  byte-identical. They were duplicated with nothing keeping them in sync, so a
  knob documented in one could quietly go missing from the other.

### Changed
- Solver checkouts and build trees are cached per host instead of per workspace
  (`BTC_PUZZLE_LAB_CACHE`, else `~/.cache/btc-puzzle-lab/vendor`), and a build
  already sitting in that tree is installed as-is. A second workspace on the same
  box copies the binaries in ~87ms where it used to spend 20.7s recompiling.
  Existing workspace `vendor/` directories keep being used, so provisioned hosts
  are unaffected. `engines install --force` still rebuilds.
- The build-dependency gate only runs when something will actually be compiled,
  so reusing a cached build no longer demands `libgmp-dev` or an apt round-trip.
- `auto` no longer re-solves a known puzzle on every run to verify the engine.
  A pass is recorded in `state/selfcheck.json` against the SHA-256 of the binary
  that produced it — and, for GPU engines, the detected compute capability — so a
  rebuild, a new commit or a different card earns a fresh check while an unchanged
  build does not. `engines selfcheck` always searches for real.
- Kangaroo builds with `make -j` (13s to 3s on four cores). keyhunt and BitCrack
  stay serial deliberately: their recipes are shell command lists, so make has
  nothing to schedule — which is why artifact reuse, not parallelism, was the fix.
- Kangaroo and BitCrack no longer `make clean` before every build. BitCrack still
  cleans when its Makefile is retargeted at a different CUDA toolkit or card,
  which is when stale objects would actually be linked in.
- CLI startup no longer imports the whole program. Command modules load inside
  the handler that needs them, and `catalog_import` stopped pulling `requests`
  just to hold a URL constant. `--version` went from 290ms to 100ms.
- `--help` leads with `auto` and the commands around it. The rest — the layers
  `auto` drives, plus the inspection surface — still parse and run but are grouped
  by purpose behind `--help-all`. A flat 25-name wall was the first thing a new
  machine printed.
- `relay-keygen` sits in the short listing beside `hub`, which cannot start
  without the keypair it writes, and no longer suggests `hub --host 0.0.0.0`:
  the hub speaks plain HTTP and holds the key that unseals private keys, so the
  suggested bind is localhost with a pointer to the TLS setup.
- `host` is an alias of `adapt`, which was the same probe plus the advice.
- Bootstrap scripts check for Python 3.12+ up front and name an interpreter to
  install, instead of failing inside pip after apt and a clone. `china-bootstrap`
  uses `.venv` like the other scripts (was `.venv-run`).
- Every environment variable the code reads is now in `config/.env.example`.
  Fourteen were not, including the `*_REPO` mirrors `china-bootstrap.sh` relies on.

### Fixed
- `auto --plan-only` built the solver before reporting that it had not searched,
  though both its help text and the README promise it builds nothing. On a GPU box
  that was minutes of nvcc to answer which engine would be picked. It now stops at
  the decision: ~0.2s, nothing written. The test that should have caught this
  asserted the build *had* happened, under the name "stops before building".
- `auto` ignored `BTC_PUZZLE_LAB_ENGINE` / `_DP` / `_THREADS` and then overwrote
  them with its own choice for the run, while `strategy`, `run` and `plan` had
  always honoured them. `export BTC_PUZZLE_LAB_DP=30` before an `auto` run —
  which `china-bootstrap.sh` instructs — therefore did nothing. `auto` now reads
  them through the same validated path as `--engine`, reports which pin it used,
  and still lets an explicit flag outrank the environment.
- `data/puzzles.json` and `data/puzzle-tx-export.csv` were tracked in git as
  byte-identical copies of the package data, while `data/` is also where
  `import-catalog` writes — and `auto` calls that on every run. One `auto` in a
  checkout left the tree dirty (139 to 1927 lines). `data/` is gitignored output
  now; the packaged copies are the only source.

### Removed
- `audit_result_public_dict()`, unused since v0.2 and a second implementation of
  "what is safe to write out of an audit row" — a denylist (`asdict` then drop
  `hit`) beside the allowlist `export_audit_report` actually uses. A new secret
  field on the dataclass would have leaked through the copy nothing exercised.
- `paths.DATA_DIR`, an export nothing read.

## [0.7.0] — 2026-08-13

Control VPS hub on top of `auto`, plus the quality pass before deploy.

### Added
- Control VPS hub: hunt boxes `auto --relay https://<control>:8787/hit`; the
  always-on host runs `hub` to unseal, notify, and sweep (`RELAY_TOKEN`,
  `relay-keygen`). Dest stays on the hub, not on hunt machines.

### Fixed
- Kangaroo `dp` default is 30 for `plan` / `batch` / `once` / `run --auto`,
  not only for `auto`. The old tier knobs (14/16/18) still OOM a container in
  hours; `auto` had already pinned 30, the inventory-aware planner had not.
- `dest` and `--relay` cannot be set on the same box: that is the dual-sweep
  footgun (hunt dest + hub dest + live). Hunt `auto --relay` also skips the
  local sweep even if dest leaked into `.env`.
- `--relay` now requires `--relay-token` up front instead of 401-retrying the
  outbox after a hit.
- Chat notify and sealed relay are separate: `notify_hit` is Discord/Telegram
  only; hunt posts `RELAY_URL` from the search loop even with `--no-notify`.
  Hub ingest no longer needs `skip_relay`.
- `run` / `run_puzzle` default kangaroo `dp` is 30 (was 16).
- `doctor` gpu_solver accepts RCKangaroo, not only BitCrack.

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
