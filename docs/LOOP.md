# Full loop (`once`)

Closed-loop path for this lab:

```text
sync unsolved catalog → host strategy → one resource slot → search
        → hit audit → optional sweep (dry-run / gated live)
```

This is the product path. It is **not** the public btcpuzzle.info pool client.

## Commands

```bash
btc-puzzle-lab once
# typical 5090 focus (holds GPU until BitCrack exits / you Ctrl-C):
btc-puzzle-lab once --ids 71 --resource gpu

# budgeted VPS session (stops solvers at the wall clock):
btc-puzzle-lab watch --ids 71 --resource gpu --max-hours 6
```

Defaults for `once` / `watch`:

| Flag | Default | Meaning |
|---|---|---|
| sync | on | `import-catalog` from bundled export |
| `--status` | `unsolved` | board filter |
| `--bits-min` | `32` | skip tiny practice noise |
| `--limit` | `1` | one puzzle per host pass |
| `--resource` | `auto` | `gpu` on GPU hosts, else `cpu` |
| stop-on-hit | on | stop the slot after a hit |
| audit | on | verify address↔key |
| transfer | on | call `sweep_hit` (still disabled/dry-run unless `.env` says otherwise) |
| `--max-seconds` | off | SIGTERM external solver after N seconds |

`watch` extras: `--max-hours`, `--max-passes`, `--idle-sleep`, `--sync-every`.

## Resource model

| Queue | Engines | Concurrent jobs / machine |
|---|---|---|
| `gpu` | BitCrack, RCKangaroo | **1** |
| `cpu` | keyhunt, kangaroo, sequential, window | **1** recommended |

Do not split one GPU across multiple unsolved address searches.

## Safety

Transfer uses the existing policy in [TRANSFER.md](TRANSFER.md):

- `AUTO_TRANSFER_ENABLED=false` by default → sweep reports `skipped`
- dry-run until you explicitly enable live confirm
- `once --no-transfer` if you only want search + audit

## Hit notifications

Optional alerts after a hit (no private keys in the payload):

```bash
# in config/.env
NOTIFY_ENABLED=true
NOTIFY_WEBHOOK_URL=https://ntfy.sh/your-topic   # or Discord/Slack webhook
# and/or:
# NOTIFY_TELEGRAM_BOT_TOKEN=...
# NOTIFY_TELEGRAM_CHAT_ID=...
```

`once` / `watch` send notify automatically when enabled. Disable per-run with `--no-notify`.
Payload includes puzzle id, address, engine, audit/transfer status only.

## Long GPU runs

External solvers now stream redacted logs (no full stdout buffering) and honor
`--max-seconds` / `watch --max-hours`. Private-key lines are redacted in the
console; hits still land in ignored `state/HITS.jsonl`.

## Related

- Machine bootstrap: [MACHINE.md](MACHINE.md)
- Transfer ops: [TRANSFER.md](TRANSFER.md)
