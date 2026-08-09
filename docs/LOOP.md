# Solved-practice loop (`once`)

The default loop stays inside the packaged, already-solved practice catalog:

```text
packaged solved practice → host strategy → one resource slot → local search
        → address/key audit → stop
```

Catalog sync, notifications, and transfer are explicit opt-ins. They are not
part of the Runpod benchmark path.

## Commands

```bash
# safe defaults: no catalog sync, solved only, no transfer, no notifications
btc-puzzle-lab once

# explicit bundled practice case
btc-puzzle-lab once --ids 20 --resource cpu --no-progress

# bounded paid-GPU throughput + checkpoint validation
btc-puzzle-lab benchmark-gpu --seconds 90
```

Defaults for `once` / `watch`:

| Control | Default | Meaning |
|---|---|---|
| catalog sync | off | use the packaged practice set or existing local catalog |
| `--status` | `solved` | select already-solved practice entries |
| `--bits-min` | `1` | include small correctness fixtures |
| `--limit` | `1` | one puzzle per host pass |
| `--resource` | `auto` | `gpu` on GPU hosts, otherwise `cpu` |
| stop-on-hit | on | stop the slot after a practice hit |
| audit | on | verify address/key correspondence locally |
| transfer | off | requires explicit `--transfer` and separate policy gates |
| notifications | off | requires explicit `--notify` and local configuration |
| `--max-seconds` | off | optional SIGTERM bound for an external practice solver |

`--sync-catalog` explicitly imports public metadata before planning. Importing
metadata does not establish authorization to search an address. Runpod examples
do not use that flag or any unsolved/funded address.

## Synthetic GPU path

`benchmark-gpu` is separate from `once` and `watch`. It accepts only
`--seconds` (75–90) and `--no-progress`; it accepts no address, catalog ID,
keyspace, transfer, or notification options. It runs two rounds so the second
must resume the cursor written by the first.

Always pair the process bound with a provider-side Pod termination deadline.
Stopping the Python process does not stop or delete the cloud Pod.

## Resource model

| Queue | Practice engines | Concurrent jobs / machine |
|---|---|---|
| `gpu` | BitCrack, RCKangaroo | **1** |
| `cpu` | keyhunt, Kangaroo, sequential, window | **1** recommended |

Do not split one GPU across multiple benchmark processes. Do not use GitHub
Actions, Codespaces, or self-hosted Actions to build or run the solvers.

## Related

- Runpod benchmark: [MACHINE.md](MACHINE.md)
- Acceptable use: [../SECURITY.md](../SECURITY.md)
- Separately gated transfer module: [TRANSFER.md](TRANSFER.md)
