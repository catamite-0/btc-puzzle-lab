# BTC Puzzle Lab

Practice lab for [Bitcoin Puzzle Transaction](https://privatekeys.pw/puzzles/bitcoin-puzzle-tx) workflows.

Goal of v0.1: run a small end-to-end pipeline on this host (2 CPU / 2 GiB):

```text
catalog → search engine → state/HITS.jsonl → local audit → optional sweep transfer
```

This is an educational workflow lab. It does **not** promise unsolved-puzzle breakthroughs, mining income, or production key custody.

## Practice catalog

| ID | Bits | Default engine | Notes |
|---:|-----:|---|---|
| 20 | 20 | `sequential` | Full-range scan is practical here |
| 40 | 40 | `window` | Full-range is not practical on this host; practice uses a narrow window |

## Setup

```bash
python3 -m venv .venv-dev
source .venv-dev/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install -e .
cp config/.env.example config/.env   # only needed for auto-transfer
```

## Commands

```bash
# list practice puzzles
python -m btc_puzzle_lab list

# verify catalog solution derives the address (no search)
python -m btc_puzzle_lab verify 20

# real sequential search for puzzle #20 → append HITS
python -m btc_puzzle_lab run 20

# puzzle #40 practice window (default engine)
python -m btc_puzzle_lab run 40

# optional: inject known solved key just to exercise HITS → audit
python -m btc_puzzle_lab run 40 --engine inject-known

# audit recorded hits (address re-derivation)
python -m btc_puzzle_lab audit

# optional on-chain balance check (mempool.space)
python -m btc_puzzle_lab audit --balance

# after configuring config/.env, sweep latest hit (default dry-run)
python -m btc_puzzle_lab transfer

# or transfer immediately after a hit
python -m btc_puzzle_lab run 20 --transfer
```

Private keys are written only under ignored `state/HITS.jsonl` (`0600`). CLI does not print them unless you pass `--show-key`.

## Auto-transfer safety gates

Configured via `config/.env` (see `config/.env.example`):

| Gate | Default | Meaning |
|---|---|---|
| `AUTO_TRANSFER_ENABLED` | `false` | Master switch |
| `AUTO_TRANSFER_DRY_RUN` | `true` | Sign + write local `.txhex`, do **not** broadcast |
| `AUTO_TRANSFER_DEST_ADDR` | empty | Destination must be a valid BTC address |
| `AUTO_TRANSFER_LIVE_CONFIRM` | empty | Live broadcast requires exact phrase `I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC` |
| fee / dust caps | set | Fee rate is capped; dust / min-balance skips apply |

Dry-run artifacts land in ignored `state/dryrun_*.txhex` (`0600`). Signed hex is never printed to stdout. Address↔key mismatch aborts hard.

Supports sweeping compressed/uncompressed Legacy P2PKH and compressed Native Segwit P2WPKH.

### Optional Keyhunt

If you already have a Keyhunt binary (for example from `coinsense`):

```bash
export KEYHUNT_PATH=/home/dev/projects/coinsense/bin/keyhunt
python -m btc_puzzle_lab run 20 --engine keyhunt
```

## Validate

```bash
python -m ruff check src tests
python -m pytest
```

## Scope boundaries

- Independent from `coinsense` (no Discord / Gemini; own transfer module).
- Live broadcast is opt-in only behind explicit confirm.
- Unsolved high-bit puzzles remain unrealistic on this host class.
- Solved-puzzle balances are usually already spent; transfer dry-runs still exercise the pipeline.

## Local state

| Path | Purpose |
|---|---|
| `state/HITS.jsonl` | Hits (gitignored, mode `0600`) |
| `state/dryrun_*.txhex` | Dry-run signed txs (gitignored, mode `0600`) |
| `config/.env` | Local transfer config (gitignored) |
| `data/puzzles.json` | Practice catalog |

## License

MIT
