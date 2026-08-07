# BTC Puzzle Lab

Practice lab for **already-solved** [Bitcoin Puzzle Transaction](https://privatekeys.pw/puzzles/bitcoin-puzzle-tx) entries.

Goal of v0.1: run a small end-to-end pipeline on this host (2 CPU / 2 GiB):

```text
catalog → search engine → state/HITS.jsonl → local audit
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
```

Private keys are written only under ignored `state/HITS.jsonl` (`0600`). CLI does not print them unless you pass `--show-key`.

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

- Independent from `coinsense` whale-hunting / auto-transfer.
- No Discord, no Gemini, no automatic broadcasting.
- Unsolved high-bit puzzles are out of scope for this host class.
- Solved-puzzle balances are usually already spent; audit balance checks are for pipeline practice only.

## Local state

| Path | Purpose |
|---|---|
| `state/HITS.jsonl` | Practice hits (gitignored, mode `0600`) |
| `data/puzzles.json` | Practice catalog |

## License

MIT
