# BTC Puzzle Lab — agent notes

- Purpose: puzzle workflow lab (`search → HITS → audit → optional sweep`) with a
  first-class solver toolchain (`engines install`). Focus on engineering:
  algorithms, catalog, automation, and solver wiring.
- Keep independent from `coinsense` Discord / Gemini; local transfer module is allowed.
- Auto-transfer defaults: disabled + dry-run. Live broadcast requires
  `AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC`.
  Post-hit ops: `docs/TRANSFER.md` (dry-run → verify → live / broadcast-dry-run).
- Hit notify: `NOTIFY_ENABLED` + webhook/Telegram; payloads must never include
  private keys or signed tx hex. Keep independent from coinsense Discord/Gemini.
- Never commit `state/`, `config/.env`, `dist/`, or hit/dry-run files.
- Do not print private keys or signed tx hex in chat, logs, commits, or PR text.
  CLI may show keys only with explicit `--show-key`.
- Catalog ships inside the package (`btc_puzzle_lab/data/puzzles.json`); keep
  top-level `data/puzzles.json` in sync when editing the practice set.
- Full catalog: `import-catalog` writes workspace `data/puzzles.json` from
  bundled `data/puzzle-tx-export.csv` (keep CSV copies in sync under `data/` and
  `src/btc_puzzle_lab/data/`). Do not commit a full-catalog override unless intentional.
- Automation: `host` / `adapt` → `engines install` → `once` / `watch`
  (or `plan` → `batch` → `status`). Full loop docs: `docs/LOOP.md`.
- Resource model: one machine occupies one scarce slot (`gpu` or `cpu`);
  GPU VPS default is exclusive single-puzzle (`once --limit 1`).
- Solver toolchain: `btc-puzzle-lab engines install` builds keyhunt + kangaroo
  (+ BitCrack when `nvcc` is present) into ignored `vendor/` + `bin/` and writes
  `config/engines.env`. Never commit those build outputs. RCKangaroo stays manual.
  BitCrack makefile gets detected `COMPUTE_CAP` plus dual SASS/PTX gencode (5090/`sm_120`).
- Machine bootstrap: `scripts/machine-bootstrap.sh` + `docs/MACHINE.md`; preflight via `doctor`.
- Validate with: `.venv-dev/bin/python -m ruff check src tests` and
  `.venv-dev/bin/python -m pytest`.
- Ship: merge to `main`, bump versions (`pyproject.toml` + `__version__` + CHANGELOG),
  then tag `v0.5.0` (or next) to trigger `.github/workflows/release.yml`.
- Cloud Agent bootstrap: `.cursor/environment.json` + `scripts/cloud-install.sh`
  (idempotent venv/deps). Do not bake `config/.env`, `state/`, `vendor/`, or `bin/`
  into builds.
