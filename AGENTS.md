# BTC Puzzle Lab — agent notes

- Purpose: solved-puzzle practice lab (`search → HITS → audit`) with a
  first-class solver toolchain (`engines install`). Focus on engineering,
  correctness fixtures, and safe solver wiring.
- Hard boundary: local examples use only catalog entries whose included solved
  key verifies before execution. Paid GPU work uses only the fresh random
  `benchmark-gpu` target. Never search an unsolved, funded, or third-party target.
- GitHub Actions, Codespaces, and self-hosted Actions are CPU lint/test/release
  paths only. Never build or execute a solver there.
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
- Full catalog metadata: `import-catalog` writes workspace `data/puzzles.json` from
  bundled `data/puzzle-tx-export.csv` (keep CSV copies in sync under `data/` and
  `src/btc_puzzle_lab/data/`). It is not a Runpod execution queue. Do not commit
  a full-catalog override unless intentional.
- Automation: `host` / `adapt` → `engines install` → `once` / `watch`
  for solved practice (or `plan` → `batch` → `status`). Loop docs: `docs/LOOP.md`.
- Resource model: one machine occupies one scarce slot (`gpu` or `cpu`);
  GPU VPS default is exclusive single-puzzle (`once --limit 1`).
- Solver toolchain: `btc-puzzle-lab engines install` builds keyhunt + kangaroo
  (+ BitCrack when `nvcc` is present) into ignored `vendor/` + `bin/` and writes
  `config/engines.env`. Never commit those build outputs. RCKangaroo stays manual.
  BitCrack makefile gets detected `COMPUTE_CAP` plus dual SASS/PTX gencode (5090/`sm_120`).
- Machine bootstrap: `scripts/machine-bootstrap.sh` + `docs/MACHINE.md`; preflight
  via `doctor`. Paid GPU validation uses only `benchmark-gpu --seconds 90`.
- Validate with: `.venv-dev/bin/python -m ruff check src tests` and
  `.venv-dev/bin/python -m pytest`.
- Ship: merge to `main`, bump versions (`pyproject.toml` + `__version__` + CHANGELOG),
  then tag `v0.5.0` (or next) to trigger `.github/workflows/release.yml`.
- Cloud Agent bootstrap: `.cursor/environment.json` + `scripts/cloud-install.sh`
  (idempotent venv/deps). Do not bake `config/.env`, `state/`, `vendor/`, or `bin/`
  into builds.
