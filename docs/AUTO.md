# `auto` — one command, unattended

Three settings and a puzzle id. Everything between them and a running search is
derived rather than typed.

```bash
btc-puzzle-lab auto 140 \
    --dest bc1qyour-payout-address \
    --notify https://ntfy.sh/your-topic
```

Later runs need only the id — `--dest` and `--notify` are stored in `config/.env`:

```bash
btc-puzzle-lab auto 140
```

## What it does

| Stage | Action |
|---|---|
| `config` | Validate and persist payout address + alert channel (`config/.env`, mode `0600`) |
| `catalog` | `import-catalog` so ids outside the practice subset resolve |
| `host` | Probe CPU / RAM (cgroup-aware) / GPU |
| `engine` | Pick the algorithm from the **target and the hardware** |
| `target` | Refuse to spend compute on a prize that is already swept |
| `toolchain` | Install build deps, clone at a pinned commit, compile, then solve a known-answer puzzle to prove it works |
| `run` | Hand off to the watch loop with the engine and `dp` pinned |

Each stage prints before the next starts, so a failure names the step:

```text
[1/7] config    [ok] wrote 5 key(s) to /root/btc-puzzle-lab/config/.env; sweep dest=bc1q… mode=dry-run; notify=webhook
[2/7] catalog   [ok] 160 puzzles (78 unsolved, 88 with pubkey); #140 bits=140 status=unsolved pubkey=yes
[3/7] host      [ok] tier=gpu cpus=64 mem_mb=116000 gpu=NVIDIA GeForce RTX 5090
[4/7] engine    [ok] engine=rckangaroo resource=gpu dp=30 build=required — 140-bit target with a known pubkey — GPU kangaroo is the fastest engine here
[5/7] target    [ok] 1QKEDNZ… still holds its prize
[6/7] toolchain [ok] rckangaroo: built and installed to bin/RCKangaroo (kernels: kernel_sm120.cubin); self-check solved #40 in 3.2s
[7/7] run       [ok] watch --ids 140 --resource gpu engine=rckangaroo dp=30 plan=state/plan_140.json
```

## How the engine is chosen

From the target and the hardware only — never from which binaries happen to be
installed. (Reading inventory is what once moved puzzle #160 from the CPU queue
to the GPU queue just because RCKangaroo had been built; see
[ARCHITECTURE.md](ARCHITECTURE.md) §5.)

| Target | Host | Engine | Slot |
|---|---|---|---|
| range ≤ 2M keys | any | `sequential` (built in) | cpu |
| known pubkey, ≥32 bits | GPU + CUDA | `rckangaroo` | gpu |
| known pubkey, ≥32 bits | no GPU | `kangaroo` | cpu |
| address only | GPU + CUDA | `bitcrack` | gpu |
| address only | no GPU | `keyhunt` | cpu |

A known public key buys a square-root speedup, so kangaroo-class engines win
outright when the export carries one — regardless of how fast the card is.

**GPU present but no CUDA toolkit is reported, not worked around:**

```text
[4/7] engine    [!!] blocked: GPU detected (RTX 5090) but no CUDA toolkit, so rckangaroo cannot be built
  remedy: install the CUDA toolkit so nvcc is on PATH …; or pass --allow-cpu-fallback to run kangaroo on the CPU instead
```

Silently relocating GPU work to the CPU changes the expected time to solve by
orders of magnitude, so it stays an explicit decision.

## What it sets for you

**`dp=30` for kangaroo-class runs.** The engine default of 16 grows the
distinguished-point table about 35 GB/h; against a 116 GB container that is an
OOM kill roughly every 3.4 hours, and a kangaroo restart discards the entire
table. Across dp 23–32 the extra algorithmic work is under 0.003%, so the
largest survivable value is the safe pick, not a gamble. Override with `--dp`.

**A per-target job board** (`state/plan_<id>.json`), so two `auto` runs on one
box do not overwrite each other's plan.

## Money safety

`--dest` accepts Legacy (`1…`), P2SH (`3…`), Native Segwit (`bc1q…`) and Taproot
(`bc1p…`) payout addresses. It turns auto-transfer **on in dry-run**: a hit is
verified, signed, and written to `state/dryrun_*.txhex` — and *not* broadcast.

Broadcasting real BTC is a separate, explicit decision:

```bash
btc-puzzle-lab auto 140 --dest bc1q… --live
```

`--live` writes `AUTO_TRANSFER_LIVE_CONFIRM=I_UNDERSTAND_THIS_BROADCASTS_REAL_BTC`
and clears the dry-run flag. Everything else in
[TRANSFER.md](TRANSFER.md) still applies: fee caps, dust floors, confirmed-only
UTXOs, and a hard abort when the address does not match the key.

Inspect a dry-run artifact before ever going live:

```bash
btc-puzzle-lab verify-dry-run state/dryrun_*.txhex --check-dest
```

## Useful flags

| Flag | Why |
|---|---|
| `--plan-only` | Show the engine decision and stop — no build, no search |
| `--engine NAME` | Pin the engine instead of deriving it |
| `--allow-cpu-fallback` | Run the CPU engine when the GPU has no CUDA toolkit |
| `--ignore-swept` | Search even if the prize is already claimed |
| `--dp N` / `--threads N` | Override the derived knobs |
| `--max-hours N` | Stop at a wall clock (matches a rental window) |
| `--max-seconds N` | Recycle the solver each pass (safe only for free-restart engines) |
| `--no-build` | Assume the solver is already installed |
| `--no-install-deps` | Never invoke apt/dnf; print the install line instead |
| `--no-selfcheck` | Skip the known-answer solve (leaves the engine unverified) |

`--max-seconds` is a bad idea for `kangaroo` / `rckangaroo`: their restart is
destructive, so recycling throws away the table every pass.

## Split: hunt box vs control VPS

Restricted hunt boxes should not sweep or talk to Discord. Run this lab as an
always-on **control VPS** (unseal + notify + sweep) and point `auto` at it:

```bash
# control VPS (do not set RELAY_URL)
btc-puzzle-lab relay-keygen
btc-puzzle-lab config --dest bc1q… --notify https://discord.com/api/webhooks/...
btc-puzzle-lab config --new-relay-token
btc-puzzle-lab hub --host 0.0.0.0 --port 8787

# hunt VPS — no dest, no relay-secret
btc-puzzle-lab auto 140 \
    --relay https://<control>:8787/hit \
    --relay-seal-pubkey <hex> \
    --relay-token <same-token>
```

`hub` requires `RELAY_TOKEN` (16+ chars) and `config/relay-secret`. `--relay`
on a hunt box also requires that token up front (otherwise the hub returns 401
and the outbox retries forever). Dest and `--relay` cannot be set together:
that would let hunt and hub both sweep. Hunt `auto --relay` skips the local
sweep even if a dest leaked into `.env`.

Put TLS in front and firewall the port. Notify from the hub uses `skip_relay`
so it does not POST back to itself. Live broadcast still needs `--live` / the
confirm phrase **on the control VPS**.

## Build dependencies

Missing compilers and headers are installed automatically via `apt-get` or `dnf`
when the process is root or has passwordless `sudo`. Otherwise the exact command
is printed and the run stops:

```text
missing build dependencies: gmp.h
  Debian/Ubuntu: sudo apt install -y git build-essential libgmp-dev
  Fedora/RHEL:   sudo dnf install -y git gcc-c++ make gmp-devel openssl-devel
```

`sudo -n` is used on purpose — an unattended bring-up must fail with a readable
message rather than block on a password prompt nobody will type.

CUDA is never installed automatically: it involves drivers and vendor repos, so
it is reported as a blocker with a remedy instead.

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Ran to completion (or `--plan-only` succeeded) |
| 1 | A stage failed — engine blocked, prize gone, build failed, transfer error |
| 2 | Bad configuration (invalid payout address, unknown engine) |

## When not to use it

`auto` is the opinionated single-target path. For a catalog-wide board, several
targets, or manual control of each step, use `plan` → `batch` → `status`, or
`once` / `watch` directly ([LOOP.md](LOOP.md)).

For long unattended runs, put a supervisor in front of it —
`scripts/watchdog.py` watches memory growth, restart churn and throughput decay,
none of which the loop can see about itself ([MACHINE.md](MACHINE.md)).
