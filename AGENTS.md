# BTC Puzzle Lab — agent notes

- Purpose: puzzle workflow lab (`search → HITS → audit → optional sweep`).
- Host class is 2 CPU / 2 GiB; do not treat unsolved high-bit puzzles as likely wins.
- Keep independent from `coinsense` Discord / Gemini; local transfer module is allowed.
- Auto-transfer defaults: disabled + dry-run. Live broadcast requires
  `AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC`.
- Never commit `state/`, `config/.env`, or hit/dry-run files.
- Do not print private keys or signed tx hex in chat, logs, commits, or PR text.
  CLI may show keys only with explicit `--show-key`.
- Validate with: `.venv-dev/bin/python -m ruff check src tests` and
  `.venv-dev/bin/python -m pytest`.
- Cloud Agent bootstrap: `.cursor/environment.json` + `scripts/cloud-install.sh`
 (idempotent venv/deps). Do not bake `config/.env` or `state/` into builds.

## Cursor Cloud specific instructions

- No long-running services: this is a Python 3.12 CLI. The update script
 (`scripts/cloud-install.sh`) already creates `.venv-dev` and does the editable
 install, so there is nothing to "start" — just invoke the CLI.
- Always run via the venv interpreter: `.venv-dev/bin/python -m btc_puzzle_lab <cmd>`
 (the venv is deliberately named `.venv-dev`, not `.venv`).
- Quick end-to-end smoke test of the core pipeline (no network, no secrets):
 `run 20` performs a real full-range sequential search that finds the key and
 writes a HIT, then `audit` re-derives the address. `run`/`audit`/`transfer`
 write only to gitignored `state/`; `transfer` also needs `config/.env`
 (copy from `config/.env.example`).
- `audit --balance` and `transfer` (when enabled) reach out to mempool.space, so
 they need network egress; the offline commands above are enough to verify setup.
