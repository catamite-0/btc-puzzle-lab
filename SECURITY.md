# Security

This repository is an **educational practice lab** for solved Bitcoin Puzzle
Transaction workflows. It is not a wallet, miner, or custody product.

## What ships in the catalog

`data/puzzles.json` (also packaged under `btc_puzzle_lab/data/`) includes
**public, already-solved** practice keys for small puzzles. Those values are
intentional for local exercises. They are not undiscovered keys and must not be
treated as funds under management.

## Defaults

| Control | Default |
|---|---|
| Auto-transfer | disabled |
| Transfer mode | dry-run |
| Live broadcast | blocked unless `AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC` |
| Private key printing | off (requires `--show-key`) |

## Local secrets

Never commit:

- `config/.env`
- `state/` (hits, run logs, coverage, dry-run tx hex)

Hit and dry-run files are written with mode `0600` when created by this tool.

## Reporting

If you find a way this tool prints private keys or signed transaction hex without
an explicit opt-in, open a private report via GitHub security advisories or the
repository issues page.
