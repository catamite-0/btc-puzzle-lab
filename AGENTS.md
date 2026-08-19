# BTC Puzzle Lab — agent notes

- Purpose: puzzle workflow lab (`search → HITS → audit → optional sweep`) with a
  first-class solver toolchain (`engines install`). Focus on engineering:
  algorithms, catalog, automation, and solver wiring.
- Keep independent from `coinsense` Discord / Gemini; local transfer module is allowed.
- Auto-transfer defaults: disabled + dry-run. Live broadcast requires
  `AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC`.
  Post-hit ops: `docs/TRANSFER.md` (dry-run → verify → live / broadcast-dry-run).
- Hit notify: `NOTIFY_ENABLED` + webhook/Telegram, or sealed `RELAY_URL` to the
  control VPS `hub` when those are blocked. Payloads must never include
  plaintext private keys or signed tx hex. `relay-keygen` / `hub` / `unseal`.
  Keep independent from coinsense Discord/Gemini.
- Never commit `state/`, `config/.env`, `dist/`, or hit/dry-run files.
- Do not print private keys or signed tx hex in chat, logs, commits, or PR text.
  CLI may show keys only with explicit `--show-key`.
- Catalog and CSV snapshot live in the package only
  (`src/btc_puzzle_lab/data/`), and that is the single source of truth. Edit the
  practice set there.
- Top-level `data/` is workspace output, not source: `import-catalog` writes
  `data/puzzles.json` there and `auto` calls it on every run. It is gitignored —
  it used to hold tracked byte-identical copies of the package data, so one
  `auto` run dirtied the checkout and the two copies could drift apart.
- Automation: `auto <id>` is the default hunt entry (config → catalog → host →
  engine → build+verify → watch); docs in `docs/AUTO.md`. Restricted boxes POST
  sealed hits to `hub` on the always-on control VPS. Manual path is
  `adapt` (alias `host`) → `engines install` → `once` / `watch`. Full loop docs: `docs/LOOP.md`.
- `--help` lists only `auto` and the handful around it; the layers `auto` drives
  sit behind `--help-all` (`_ADVANCED` in `cli.py`). A new command has to be put
  in one bucket or the other.
- Engine choice for `auto` lives in `recommend.py` and must stay inventory-blind:
  it reads the target and the host, never `available_engines()` (ARCHITECTURE §5).
- Resource model: one machine occupies one scarce slot (`gpu` or `cpu`);
  GPU VPS default is exclusive single-puzzle (`once --limit 1`).
- Solver toolchain: `btc-puzzle-lab engines install` builds keyhunt + kangaroo
  (+ BitCrack when `nvcc` is present) into ignored `vendor/` + `bin/` and writes
  `config/engines.env`. Never commit those build outputs. RCKangaroo stays manual.
  BitCrack makefile gets detected `COMPUTE_CAP` plus dual SASS/PTX gencode (5090/`sm_120`).
- Build cache: `vendor/` is shared per host (`BTC_PUZZLE_LAB_CACHE`, else
  `~/.cache/btc-puzzle-lab/vendor`); a build already there is reused rather than
  recompiled, and `--force` is the way past it. `bin/` stays per-workspace.
- Python 3.12+ is required (`pyproject`); bootstrap scripts preflight it through
  `scripts/lib-python.sh` rather than letting pip fail late.
- Machine bootstrap: `scripts/machine-bootstrap.sh` + `docs/MACHINE.md`; preflight via `doctor`.
- Control VPS: `scripts/control-install.sh` + `docs/DEPLOY.md`. Package only —
  never a compiler or `engines install` on the host that holds `relay-secret`.
  `hub` has no TLS and must stay bound to localhost behind a tunnel or proxy.
- Validate with: `.venv-dev/bin/python -m ruff check src tests` and
  `.venv-dev/bin/python -m pytest`.
- Ship: merge to `main`, bump versions (`pyproject.toml` + `__version__` + CHANGELOG),
  then either push the `vX.Y.Z` tag or run the Release workflow manually from the
  Actions tab and type the version. Both paths assert tag/input, `pyproject.toml`
  and `__version__` all agree before building; the manual path additionally
  refuses a side branch or an existing tag.
- Cloud Agent bootstrap: `.cursor/environment.json` + `scripts/cloud-install.sh`
  (idempotent venv/deps). Do not bake `config/.env`, `state/`, `vendor/`, or `bin/`
  into builds.
