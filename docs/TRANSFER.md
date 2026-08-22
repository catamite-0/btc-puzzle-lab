# Transfer runbook (post-hit sweep)

Operator playbook after a puzzle hit is recorded in `state/HITS.jsonl`.
Defaults stay safe: transfer disabled + dry-run.

## Prerequisites

```bash
# no checkout (wheel install)? btc-puzzle-lab config --write-example
cp config/.env.example config/.env
# edit:
#   AUTO_TRANSFER_ENABLED=true
#   AUTO_TRANSFER_DRY_RUN=true
#   AUTO_TRANSFER_DEST_ADDR=<your cold wallet>
```

On a split setup, dest lives on the **control VPS** that runs `hub`, not on hunt
boxes. Live confirm stays on that same host.

Optional knobs:

| Knob | Default | Notes |
|---|---|---|
| `AUTO_TRANSFER_CONFIRMED_ONLY` | `true` | Skip unconfirmed UTXOs |
| `AUTO_TRANSFER_MAX_FEE_SATS` | `100000` | Absolute fee ceiling |
| `AUTO_TRANSFER_MAX_FEE_RATE` | `250` | sat/vB cap |
| `AUTO_TRANSFER_RBF` | `true` | Mark inputs replaceable |

## Dry-run path (always first)

```bash
btc-puzzle-lab audit
btc-puzzle-lab transfer --puzzle <id> --verify-dry-run
# or latest hit:
btc-puzzle-lab transfer --verify-dry-run

btc-puzzle-lab verify-dry-run state/dryrun_*.txhex --check-dest
```

Checks performed:

- key derives hit address
- confirmed UTXOs only (unless `--allow-unconfirmed`)
- fee from **signed tx vsize** (not estimate-only)
- absolute fee / rate caps
- dry-run artifact is a single-output sweep to `AUTO_TRANSFER_DEST_ADDR`

Signed hex is written to `state/dryrun_<addr>_<fp16>.txhex` (`0600`) and never printed.

## Live broadcast

Only after dry-run looks correct:

```bash
# in config/.env
AUTO_TRANSFER_DRY_RUN=false
AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC

# re-sign + broadcast from hit:
btc-puzzle-lab transfer --puzzle <id>

# or broadcast an already-verified dry-run file:
btc-puzzle-lab transfer --broadcast-dry-run state/dryrun_....txhex
```

CLI prints `txid` + `chain_status` (`mempool` / `confirmed` / …).
Broadcast tries Blockstream → mempool.space → Blockcypher; duplicate mempool is treated as success.

## Fee override

```bash
btc-puzzle-lab transfer --puzzle <id> --fee-rate 20 --verify-dry-run
```

## Safety reminders

- Keep `config/.env`, `state/HITS.jsonl`, and `state/dryrun_*.txhex` off git and backups you do not trust.
- Flip `AUTO_TRANSFER_DRY_RUN` back to `true` after a live attempt.
- Practice catalog keys are public; use a real destination only for real hits.
- `AUTO_TRANSFER_DEST_ADDR` accepts `1…` (P2PKH), `3…` (P2SH), `bc1q…` (P2WPKH /
  P2WSH) and `bc1p…` (Taproot, bech32m). A v0 address carrying a bech32m checksum
  — or a v1 carrying a bech32 one — is rejected rather than tolerated, and
  witness versions above 1 are refused because their spending rules are still
  undefined.
