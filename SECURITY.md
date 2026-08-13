# Security

This repository is an **educational practice lab** for solved Bitcoin Puzzle
Transaction workflows. It is not a wallet, miner, or custody product.

## What ships in the catalog

`data/puzzles.json` (also packaged under `btc_puzzle_lab/data/`) includes
**public, already-solved** practice keys for small puzzles. Those values are
intentional for local exercises. They are not undiscovered keys and must not be
treated as funds under management.

`import-catalog` can write a full public export (including known solved keys) into
workspace `data/puzzles.json`. Treat that file as public puzzle metadata, not as
a secret store. Prefer `--no-solutions` when you do not need practice keys.

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
- `state/` (hits, run logs, coverage, batch plans, dry-run tx hex)

Hit and dry-run files are written with mode `0600` when created by this tool.
`state/batch_plan.json` stores routing metadata only (no private keys).

The control VPS (`hub`) holds `config/relay-secret` and the sweep dest. Hunt
boxes get only `RELAY_SEAL_PUBKEY` plus a shared `RELAY_TOKEN` (bearer auth).
Relay payloads, outbox rows, hub HTTP responses, and webhook/Telegram bodies
must never contain a plaintext private key. `unseal` prints the key only with
`--show-key`. Do not set `RELAY_URL` on the hub host (ingest already
`skip_relay`s to avoid a loop). Bind `hub` behind TLS and a firewall.

Host overrides (`BTC_PUZZLE_LAB_CPUS` / `_MEM_MB` / `_GPU`) affect strategy knobs
only; they are not credentials.

`engines install` clones third-party solver source into ignored `vendor/` and
copies binaries into ignored `bin/`. Those upstream licenses apply; treat build
outputs as local toolchains, not secrets — but do not commit them.

## Reporting

If you find a way this tool prints private keys or signed transaction hex without
an explicit opt-in, open a private report via GitHub security advisories or the
repository issues page.
