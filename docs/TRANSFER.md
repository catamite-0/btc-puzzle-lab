# Transfer runbook (post-hit sweep)

Operator playbook after a puzzle hit is recorded in `state/HITS.jsonl`.
Defaults stay safe: transfer disabled + dry-run.

## Prerequisites

```bash
btc-puzzle-lab config --dest <your cold wallet> --notify https://...
# or copy config/.env.example → config/.env and set AUTO_TRANSFER_DEST_ADDR=
```

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
- dry-run artifact is single-output sweep to `DEST_ADDR`

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
- Flip `DRY_RUN` back to `true` after a live attempt.
- Practice catalog keys are public; use a real destination only for real hits.
