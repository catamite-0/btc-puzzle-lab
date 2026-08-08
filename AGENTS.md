# BTC Puzzle Lab — agent notes

- Purpose: puzzle workflow lab (`search → HITS → audit → optional sweep`).
- Host class is 2 CPU / 2 GiB; do not treat unsolved high-bit puzzles as likely wins.
- Keep independent from `coinsense` Discord / Gemini; local transfer module is allowed.
- Auto-transfer defaults: disabled + dry-run. Live broadcast requires
  `AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC`.
- Never commit `state/`, `config/.env`, `dist/`, or hit/dry-run files.
- Do not print private keys or signed tx hex in chat, logs, commits, or PR text.
  CLI may show keys only with explicit `--show-key`.
- Catalog ships inside the package (`btc_puzzle_lab/data/puzzles.json`); keep
  top-level `data/puzzles.json` in sync when editing the practice set.
- Full catalog: `import-catalog` writes workspace `data/puzzles.json` from
  bundled `data/puzzle-tx-export.csv` (keep CSV copies in sync under `data/` and
  `src/btc_puzzle_lab/data/`). Do not commit a full-catalog override unless intentional.
- Automation: `host` / `adapt` → `plan` → `batch` → `status`.
- Validate with: `.venv-dev/bin/python -m ruff check src tests` and
  `.venv-dev/bin/python -m pytest`.
- Ship: merge to `main`, bump versions (`pyproject.toml` + `__version__` + CHANGELOG),
  then tag `v0.3.0` to trigger `.github/workflows/release.yml`.
- Cloud Agent bootstrap: `.cursor/environment.json` + `scripts/cloud-install.sh`
  (idempotent venv/deps). Do not bake `config/.env` or `state/` into builds.
