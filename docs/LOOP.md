# Full loop (`start` / `once`)

Product path:

```text
config dest+notify  →  start <puzzle>
    host probe → pick engine → clone/build → watch until hit
        → audit → notify → dry-run sweep (live still gated)
```

This is **not** the public btcpuzzle.info pool client.

## Commands

```bash
# once per machine (or whenever dest/notify change):
btc-puzzle-lab config --dest <your-btc-address> --notify https://ntfy.sh/your-topic

# each hunt — adapts to this host and installs the chosen solver:
btc-puzzle-lab start 71

# typical 5090 focus still works as the manual loop:
btc-puzzle-lab once --ids 71 --resource gpu

# budgeted VPS session (stops solvers at the wall clock):
btc-puzzle-lab watch --ids 71 --resource gpu --max-hours 6
```

`start` defaults:

| Step | What it does |
|---|---|
| catalog | `import-catalog` from the bundled export |
| method | `plan_strategy` for this host + puzzle |
| install | clone/build that engine into `bin/` if missing |
| run | `watch` that puzzle until a hit (`--once` for a single pass) |
| hit | audit → notify (no keys) → sweep to dest (dry-run unless `--live`) |

`once` / `watch` flags are unchanged:

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
- `start` / `config --dest` turns transfer **on** (still dry-run until `--live`)
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
