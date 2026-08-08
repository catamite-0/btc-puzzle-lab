# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- `import-catalog` to load the full 160-puzzle Bitcoin Puzzle Transaction list
  from the bundled CSV snapshot (or `--url` / `--from-csv`)
- Catalog automation board: `plan` → `batch` → `status` (`state/batch_plan.json`)
- Environment-adaptive host tiers (`host` / `adapt`): CPU/RAM/GPU probe, knobs,
  and env overrides (`BTC_PUZZLE_LAB_CPUS` / `_MEM_MB` / `_GPU`)

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
