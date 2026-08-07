# BTC Puzzle Lab — agent notes

- Purpose: practice pipeline for **solved** Bitcoin puzzles (`search → HITS → audit`).
- Host class is 2 CPU / 2 GiB; do not treat unsolved high-bit puzzles as in-scope work.
- Keep this repo independent from `coinsense` auto-transfer / Discord / Gemini.
- Never commit `state/`, `.env`, or hit files.
- Do not print private keys in chat, logs, commits, or PR text. CLI may show them only with explicit `--show-key`.
- Validate with: `.venv-dev/bin/python -m ruff check src tests` and `.venv-dev/bin/python -m pytest`.
