# Security

This repository is an **educational practice lab** for solved Bitcoin Puzzle
Transaction workflows. It is not a wallet, miner, or custody product.

## Acceptable-use boundary

GitHub is used only to host source, documentation, bounded CPU unit tests, and
release artifacts. Do not use GitHub Actions (including self-hosted runners),
Codespaces, or other GitHub-triggered compute for keyspace searches, external
solver builds, cryptocurrency mining, or sustained workloads. The workflows are
intentionally CPU-only and bounded.

Local/CPU runnable examples are limited to catalog entries whose included solved
practice key is verified before execution. Never perform keyspace search against
an unsolved puzzle, an address with current funds, or any third-party wallet.
Paid Runpod solver work is limited to the short synthetic benchmark documented
in `docs/MACHINE.md`; solved fixtures are only verified there without searching.
This boundary follows the
[GitHub Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies).

Each synthetic GPU invocation creates a fresh CSPRNG hash target and checkpoint.
No private scalar is generated, the complete input string is not a Bitcoin
address, and the target is suppressed from terminal output. Any 160-bit hash has
a standard address encoding, so the benchmark makes the narrower, honest claim:
the random target has no known key or funds and must never be funded. The command
accepts no target, keyspace, or puzzle-ID override and is hard-limited to two
75–90 second rounds.

## What ships in the catalog

`data/puzzles.json` (also packaged under `btc_puzzle_lab/data/`) includes
**public, already-solved** practice keys for small puzzles. Those values are
intentional for local exercises. They are not undiscovered keys and must not be
treated as funds under management.

`import-catalog` can write a full public export (including known solved keys) into
workspace `data/puzzles.json`. Treat that file as public puzzle metadata, not as
a secret store or an execution queue. Prefer `--no-solutions` when you do not
need practice keys. The Runpod benchmark never imports it.

## Defaults

| Control | Default |
|---|---|
| Catalog sync in `once` / `watch` | disabled |
| Loop status | solved practice only |
| Auto-transfer | disabled |
| Hit notifications | disabled |
| Transfer mode | dry-run |
| Live broadcast | blocked unless `AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC` |
| Private key printing | off (requires `--show-key`) |

## Local secrets

Never commit:

- `config/.env`
- `state/` (hits, run logs, coverage, batch plans, dry-run tx hex)

Hit and dry-run files are written with mode `0600` when created by this tool.
`state/batch_plan.json` stores routing metadata only (no private keys).

Host overrides (`BTC_PUZZLE_LAB_CPUS` / `_MEM_MB` / `_GPU`) affect strategy knobs
only; they are not credentials.

`engines install` clones third-party solver source into ignored `vendor/` and
copies binaries into ignored `bin/`. Those upstream licenses apply; treat build
outputs as local toolchains, not secrets — but do not commit them.

## Reporting

If you find a way this tool prints private keys or signed transaction hex without
an explicit opt-in, open a private report via GitHub security advisories or the
repository issues page.
