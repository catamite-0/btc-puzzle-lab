# Changelog

All notable changes to this project are documented here.

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
