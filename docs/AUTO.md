# `auto` — one command, unattended

> This page documents the current v0.8 single-target runner plus both read-only
> planning previews: catalog-wide and pinned-target. Managed execution is not
> currently provided; [AUTOPILOT.md](AUTOPILOT.md) defines the planning boundary.

## Read-only planning preview

Inspect the complete live catalog or pin the explanation to one target before
configuring a payout or compiling anything:

```bash
btc-puzzle-lab auto --plan       # full package catalog
btc-puzzle-lab auto 140 --plan   # puzzle 140 only
```

Both forms read the package-owned 160-puzzle catalog and exact host facts. With
an id, the report explains that target; without an id, it ranks live candidates
and checks a bounded chain prefix. The report is detached: no configuration,
build, solver, notification, transfer, or execution state is created.
`--plan-only` is an alias, and execution/configuration flags are rejected rather
than silently ignored. See [AUTOPILOT.md](AUTOPILOT.md) for selection outcomes,
chain stopping rules, exit codes, and the complete read-only contract.

## Current v0.8 execution path

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
| `host` | Discover the exact CPU / RAM (cgroup-aware) / GPU resources visible to this process |
| `engine` | Pick the algorithm from the **target and the hardware** |
| `target` | Refuse to spend compute on a prize that is already swept |
| `toolchain` | Install build deps, clone at a pinned commit, compile, then solve a known-answer puzzle to prove it works |
| `run` | Hand off to the watch loop with the engine and `dp` pinned |

Each stage prints before the next starts, so a failure names the step:

```text
[1/7] config    [ok] wrote 5 key(s) to /root/btc-puzzle-lab/config/.env; sweep dest=bc1q… mode=dry-run; notify=webhook
[2/7] catalog   [ok] 160 puzzles (78 unsolved, 88 with pubkey); #140 bits=140 status=unsolved pubkey=yes
[3/7] host      [ok] tier=gpu cpus=64 usable_cpus=63 mem_mb=116000 gpus=NVIDIA GeForce RTX 5090
[4/7] engine    [ok] engine=kangaroo resource=cpu dp=30 build=required — shared planner selected the fastest viable algorithm family
[5/7] target    [ok] 1QKEDNZ… still holds its prize
[6/7] toolchain [ok] kangaroo: built and installed to bin/kangaroo; self-check solved #32 in 3.2s
[7/7] run       [ok] watch --ids 140 --resource cpu engine=kangaroo dp=30 plan=state/plan_140.json
```

## How the engine is chosen

From the target and the hardware only — never from which binaries happen to be
installed. (Reading inventory is what once moved puzzle #160 from the CPU queue
to the GPU queue just because RCKangaroo had been built; see
[ARCHITECTURE.md](ARCHITECTURE.md) §5.)

The host input comes from exact, cgroup-aware discovery. The legacy
`BTC_PUZZLE_LAB_CPUS`, `BTC_PUZZLE_LAB_MEM_MB`, and `BTC_PUZZLE_LAB_GPU`
overrides remain available to lower-level commands, but cannot invent capacity
or authorize a GPU choice for `auto`.

| Target | Host | Engine | Slot |
|---|---|---|---|
| range ≤ 2M keys | any | `sequential` (built in) | cpu |
| known pubkey, compatible range | any | `kangaroo` | cpu |
| address only | compatible GPU | `bitcrack` | gpu |
| address only | no compatible GPU | `keyhunt` | cpu |

A known public key buys a square-root speedup, so kangaroo-class engines win
outright when the export carries one. `auto` can build BitCrack when it selects
that engine and `nvcc` is available. RCKangaroo remains manual: an explicit
`--engine rckangaroo` is accepted only for a compatible target/GPU and only when
its binary has already been provisioned and can pass the self-check.
The default policy reserves one CPU core for the host. A one-CPU cgroup cannot
select a CPU algorithm, though a compatible GPU algorithm may still be usable.

CUDA is a toolchain fact, not an algorithm-selection fact. An already installed
GPU binary can run without `nvcc`; CUDA is required only when that binary must be
compiled. At that point auto stops with a remedy. If auto selected the GPU
engine itself, `--allow-cpu-fallback` permits it to choose the CPU family
instead. An explicit `--engine` or environment engine pin never falls back to a
different engine.

```text
[4/7] engine    [ok] engine=bitcrack resource=gpu device=GPU-… build=required — shared planner selected the fastest viable algorithm family
[6/7] toolchain [!!] bitcrack is not installed and nvcc is unavailable; install CUDA or use --allow-cpu-fallback
```

GPU execution currently requires one visible physical device (GPU 0).
Pre-existing multi-GPU and UUID/nonzero `CUDA_VISIBLE_DEVICES` layouts stop at
the toolchain stage until build and runner device identities can be bound end to
end. GPU self-checks are deliberately not cached.

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

**The catalog target range.** Before provisioning and execution, `auto` clears
the ambient BitCrack random/chunk mode and the RCKangaroo custom start/range
pair. Those expert controls remain available to lower-level commands, but they
cannot silently change what an `auto <id>` run searches.

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
| `--plan` / `--plan-only` | With no id, read-only full-catalog ranked preview; with an id, pinned-target explanation — no config, build, or search |
| `--engine NAME` | Pin a planner engine, still validating target and host compatibility |
| `--allow-cpu-fallback` | If auto selected a GPU engine but its binary is missing and CUDA is unavailable, choose the CPU family; explicit engine pins never fall back |
| `--ignore-swept` | Search even if the prize is already claimed |
| `--dp N` / `--threads N` | Override kangaroo DP, or keyhunt/kangaroo CPU threads; other engines do not use the generic thread knob |
| `--max-hours N` | Stop at a wall clock (matches a rental window) |
| `--max-seconds N` | Recycle the solver each pass (safe only for free-restart engines) |
| `--no-build` | Refuse to build; require the selected external solver to be already installed |
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
btc-puzzle-lab hub --host 127.0.0.1 --port 8787
# public bind: --tls-cert/--tls-key, or --host 0.0.0.0 --allow-insecure behind caddy

# hunt VPS — no dest, no relay-secret
btc-puzzle-lab auto 140 \
    --relay https://<control>:8787/hit \
    --relay-seal-pubkey <hex> \
    --relay-token <same-token>
```

`hub` requires `RELAY_TOKEN` (16+ chars) and `config/relay-secret`. `--relay`
on a hunt box also requires that token up front (otherwise the hub returns 401
and the outbox retries forever). Dest and `--relay` cannot be set together:
that would let hunt and hub both sweep. Hunt `auto --relay` / `once` / `watch`
skip the local sweep when `RELAY_URL` is set, even if a dest leaked into `.env`.

Put TLS in front and firewall the port. Hub notify is chat-only (Discord /
Telegram); it does not POST back to itself. Hunt `auto --relay` posts sealed
hits from the search loop, independent of `--no-notify`. Live broadcast still
needs `--live` / the confirm phrase **on the control VPS**.

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
| 0 | Ran to completion, or read-only `--plan` produced a selection |
| 1 | A stage failed — engine blocked, prize gone, build failed, transfer error |
| 2 | Bad request/configuration, or typed plan evidence could not be acquired safely |
| 3 | Read-only `--plan` completed without selection: pinned target blocked, catalog indeterminate, or no confirmed selectable target |

## When not to use it

`auto <id>` remains the opinionated single-target execution path. `auto --plan`
is catalog-wide but read-only; it does not create a board or start a solver. For
a persistent multi-target board or manual control of each step, use `plan` →
`batch` → `status`, or `once` / `watch` directly ([LOOP.md](LOOP.md)).

For long unattended runs, put a supervisor in front of it —
`scripts/watchdog.py` watches memory growth, restart churn and throughput decay,
none of which the loop can see about itself ([MACHINE.md](MACHINE.md)).
