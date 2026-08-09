# Full loop (`once`)

Closed-loop path for this lab:

```text
sync unsolved catalog → host strategy → one resource slot → search
        → hit audit → optional sweep (dry-run / gated live)
```

This is the product path. It is **not** the public btcpuzzle.info pool client.

## Command

```bash
btc-puzzle-lab once
# typical 5090 focus:
btc-puzzle-lab once --ids 71 --resource gpu
```

Defaults:

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

## Related

- Machine bootstrap: [MACHINE.md](MACHINE.md)
- Transfer ops: [TRANSFER.md](TRANSFER.md)
