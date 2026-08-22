# Architecture: scheduling layer

Design notes for the rebuild. Every problem listed here was observed on a real
run, and the numbers are measured on the reference host (RTX 5090, 64 cores,
116 GB cgroup limit) unless stated otherwise.

## 1. What this repository actually is

The high-throughput solvers—keyhunt, Kangaroo, BitCrack and RCKangaroo—are
independent upstream projects. The lab also contains small built-in reference
searchers, but its main job is the **integration and scheduling layer** around
the external engines.

That distinction matters because it locates the defects. Every bug found in the
hardening pass sat in the seam between the orchestrator and a solver, never in
the search math:

| Defect | Symptom | Layer |
|---|---|---|
| keyhunt hit file named `KEYFOUNDKEYFOUND.txt` | solved, reported "no hit" | result parsing |
| BitCrack rows are unlabelled `<addr> <key> <pub>` | solved, reported "no hit" | result parsing |
| RCKangaroo loads `kernel_sm*.cubin` from cwd | spins at 0 MKeys/s, never exits | process setup |
| Solvers refresh progress with `\r` | 20 hours with no throughput record | output handling |
| `dp` unrelated to memory | OOM-killed every 3.4 h, zero cumulative progress | scheduling |
| Host probe read the host's RAM, not the cgroup | planned against 377 GB, had 116 GB | scheduling |

So "mature the algorithm layer, then productise" inverts the problem. The
algorithms are mature. The integration is what needs to become dependable.

## 2. Principles

1. **Dependencies point one way.** Policy must not read inventory; compute must
   not write files; adapters must not print.
2. **Make implicit knowledge into data.** Restart semantics, memory growth and
   tuning limits belong in a descriptor the scheduler can reason over, not in an
   operator's memory.
3. **Derive knobs, do not tabulate them.** Parameters are a function of
   `(target, engine, host, policy)`. A per-puzzle config table rots the moment
   the hardware changes.
4. **Prefer admission control to recovery.** For engines that accumulate state,
   a mid-flight correction *is* the failure, because it requires a restart.
5. **Anything unverifiable will eventually be wrong.** Ship a known-answer check
   with every adapter, and keep raw samples behind every measurement.

## 3. Layers

```text
┌──────────────────────────────────────────────┐
│  shell         CLI · product UI · HTTP API   │  replaceable
├──────────────────────────────────────────────┤
│  orchestration policy · scheduler · supervisor│  replaceable
├──────────────────────────────────────────────┤
│  adapters      argv · parsing · capability   │  ★ the durable asset
├──────────────────────────────────────────────┤
│  domain        Target · Range · Hit (data)   │
└──────────────────────────────────────────────┘
         ports ↑  EventSink · Storage · Clock (injected downward)
```

Today the direction is inverted in three places, all of which produced bugs:

```text
strategy → engines → paths      policy reads inventory, then the environment
search   → hits, runlog         the compute path writes fixed files
engines  → catalog (15 refs)    the adapter is bound to the domain model
```

`adapters` must not import `catalog` or `paths`, and must not `print`. It takes
a `Target` (range bounds, address, optional pubkey) plus an `EventSink`, and
returns structured results. That is what makes it embeddable in a different
shell, and it is the only layer worth carrying forward verbatim.

## 4. Engine capability descriptor

The scheduler cannot make good decisions about engines it knows nothing about.

```python
@dataclass(frozen=True)
class EngineCapability:
    name: str
    needs: Literal["address", "pubkey"]
    resource: Literal["cpu", "gpu"]
    min_range_bits: int
    max_range_bits: int | None        # RCKangaroo refuses above 170
    restart: Literal["free", "wasteful", "destructive"]
    state_growth: StateGrowth | None  # how working set grows with time
    knobs: tuple[Knob, ...]           # name, valid range, source
    selfcheck: SelfCheckSpec          # a puzzle whose answer is known
```

Filled in from measurement:

| Engine | needs | resource | restart | state growth |
|---|---|---|---|---|
| `sequential` | address | cpu | free (checkpointed) | none |
| `keyhunt` | address | cpu | wasteful (rescans) | none |
| `bitcrack` sequential | address | gpu | wasteful | none |
| `bitcrack` random window | address | gpu | **free** (fresh start is the point) | none |
| `kangaroo` | pubkey | cpu | **destructive** | DP table |
| `rckangaroo` | pubkey | gpu | **destructive** | DP table, `rate/2^dp` |

The scheduler reads this rather than guessing:

- `restart == "destructive"` → never schedule a periodic recycle, never set a
  wall-clock budget that would discard accumulated state.
- `restart == "free"` → short passes are good. BitCrack random-window mode
  *wants* a fresh uniform start each pass.
- `state_growth is not None` → the target's memory must be admitted before
  launch, not observed afterwards.

## 5. Policy and inventory are separate

The original bug: installing RCKangaroo silently moved puzzle #160 from the CPU
queue to the GPU queue because the strategy derived resource class from local
engine inventory.

```python
select_algorithm(target, policy) -> AlgorithmChoice   # pure, no IO
resolve_runtime(choice, inventory, host) -> Runtime | Blocked
```

The resource class follows from the algorithm family, never from which binaries
happen to be present. A missing binary yields `Blocked` with an explicit
downgrade option; it must not quietly relocate work to another resource.

## 6. Resources are three different things

The current model — one `cpu | gpu` slot — collapses three unlike resources.

**GPU is exclusive, not a slot count.** Two RCKangaroo instances on one card
measure ~8.0 GKeys/s each versus 17.6 alone. Total throughput is roughly
conserved, so sharing is neutral for brute force but *negative* for accumulating
engines: each instance must independently reach collision scale, so halving both
rates roughly doubles both expected times. Accumulating engines therefore claim
the device exclusively.

**CPU is a count of workers of engine-specific width, not a pool of cores.**
JeanLucPons Kangaroo on this host: 20 threads → 123.0 MK/s, 32 → 99.0, 48 → 83.9,
62 → 73.0. One process cannot absorb 62 cores. Extra workers only help on
*different* targets; multiple instances on the same target re-create the sharing
problem, since they do not share a DP table.

**Memory is time-integrated.** For kangaroo-class engines the working set grows:

```text
state(t) ≈ throughput / 2^dp × entry_bytes × t
```

At `dp=16` that is 34.6 GB/h against a 116 GB limit: 0.4 GB at launch, dead in
3.4 hours. Any instantaneous "is there room?" check passes. The question has to
be "integrated over the intended horizon, will there be room?"

Memory is also the one resource with **no safe mid-flight correction**: changing
`dp` requires a restart, and for a destructive-restart engine the restart is the
loss. Alerts can only report; the decision has to happen at admission.

## 7. Admission control

```text
admit(job, horizon T):
  1. exclusive claim   accumulating + gpu → sole occupant, else reject
  2. width allocation  workers ≤ (cores − reserved) / W_engine
                       reserved = supervisor + one feeder thread per GPU job
  3. memory integral   Σ projected_state(j, T) < limit × safety
                       unsatisfied → re-derive dp, then reject if still unmet
  4. freeze            knobs are fixed for the lifetime of the run
```

When several targets contend for one device, rank by expected time to solve
rather than by id or arrival:

| Target | Engine | Throughput | Expected |
|---|---|---|---|
| #135 | rckangaroo | 17.5 G | ~306 years |
| #71 | bitcrack | 4.37 G | ~4,285 years |
| #160 | rckangaroo | 17.5 G | ~4.75e8 years |

This reproduces the allocation chosen by hand, which is the point: the rule
encodes "a known pubkey buys a square-root speedup" so nobody has to re-derive
it per target.

Deliberately excluded: **preemption** (evicting an accumulating job destroys its
table), **fair sharing** (splitting one card is negative-sum), and **runtime knob
changes** (they imply a restart).

## 8. Calibrate → derive → admit

The lower-level runner still derives several parameters from host tiers, such as
thread counts and the safe distinguished-point default. The tier abstraction
compresses a host into four buckets, while the real constraints are continuous
and coupled to the target.

```text
calibrate(once per host)  →  calibration table
                                   ↓
    target + host + policy  →  derive()  →  knobs  →  admit()  →  launch
```

### Calibration measures only what cannot be computed

| Quantity | Why measured | This host |
|---|---|---|
| `thread_peak` | hashtable lock contention, no formula | kangaroo: 20 |
| `gpu_grid` | theoretical SM multiples are not optimal | BitCrack: 340/256/512 |
| `throughput` | feeds dp derivation and the supervisor baseline | RCKangaroo: 17.5 G |
| `dp_entry_bytes` | implementation detail | 39 bytes |
| `kangaroo_count` | chosen by the engine | 1,001,472 |

Sweeps should be geometric with refinement (the thread curve is unimodal), not
linear scans. Results are keyed by a fingerprint of
`(GPU model, cores, memory limit, engine commit)`; any change marks the table
stale. With no table, fall back to conservative defaults **and say so** — silent
magic numbers are how this layer got into trouble.

### Derivation is a pure function, and conflicts are explicit

```text
memory floor   dp ≥ log2(throughput × horizon / (budget / entry_bytes))
algorithm cap  dp ≤ (bits−1)/2 − log2(kangaroos) + log2(K) − margin
```

Worked for #135 over a 7-day horizon: floor 22.2 → `dp ≥ 23`; cap 46.8, so the
engine's own maximum of 32 binds first. Any dp in [23, 32] is safe, and the
algorithmic cost across that whole span is negligible:

| dp | growth | survives | extra work |
|---|---|---|---|
| 16 | 34.6 GB/h | 2.3 h | 0.000000% |
| 23 | 0.27 GB/h | 13 days | 0.000005% |
| 30 | 0.0021 GB/h | 4.4 years | 0.000634% |
| 32 | 0.0005 GB/h | 17.5 years | 0.002534% |

Because the cost is effectively zero on a wide range, prefer the largest dp the
constraints allow.

The two bounds can also **cross**. On a 70-bit range the algorithmic cap is ~4.8
while the engine's minimum is 14: no valid dp exists. That is not a tuning
failure — it means a million-kangaroo GPU solver is the wrong engine for that
target. The same derivation that picks parameters therefore also rejects bad
engine/target pairings, which is a scheduling decision the current code cannot
express.

Note what dp does **not** buy. Completing #135 needs roughly 5.9 TB of DP storage
at dp=30 regardless. Raising dp makes the run *survivable*, not the search
*feasible*.

### Closing the loop on calibration

A calibration table can be wrong. During this work a `grep -v GPU` that silently
failed to exclude Kangaroo's zeroed GPU column halved every CPU throughput
number, and nothing caught it — a whole tuning table was published from bad data.

Two defences:

1. **Keep raw samples**, not just the summary, so a parsing error stays auditable.
2. **Let the supervisor cross-check.** It already tracks a live throughput
   baseline; comparing that against the calibrated expectation turns a stale or
   mis-measured table into an alert instead of a silent bias.

## 9. Supervision as a contract

The watchdog in `scripts/watchdog.py` is external because nothing inside the loop
watches the loop. It should become a contract each running job satisfies:

```python
@dataclass
class JobHealth:
    uptime_s: float
    throughput: float | None      # from Progress events
    state_bytes: int | None       # RSS / DP table
    restarts_24h: int
    restart_budget: int           # derived from restart semantics + pass length
```

Rules are then predicates over health snapshots rather than hardcoded checks, so
a new engine is covered automatically. The restart budget must come from the
capability descriptor: a job that recycles by design (`--max-seconds`) is not
churning, and alerting on it teaches operators to ignore the channel.

## 10. Migration

Each phase merges and reverts independently; the existing test suite is the net.

| Phase | Change | Payoff |
|---|---|---|
| 1 | extract `adapters` package: capability descriptors, `EventSink`, no fixed paths | importable by a product shell; the self-check travels with it |
| 2 | `knobs.py` — pure `derive()` with explicit conflict rejection | testable; kills the magic-number tables |
| 3 | `admission.py` + scheduler consumes capabilities; fold in supervision | restart semantics and memory budgets become automatic |
| 4 | CLI thins out; product shell attaches alongside | two shells over one core |

Phase 2 can be written and tested **before it is wired in**: assert that
`derive()` reproduces the values reached by hand here — `dp=30`, 20 threads,
BitCrack 340/256/512. If it cannot reproduce them, the model is still wrong and
nothing has been risked.

## 11. Open questions

- **Admission horizon.** dp follows directly from it; 7 days is a placeholder and
  should match how long the host is actually rented.
- **Cross-resource downgrade.** Falling back from `rckangaroo` to `kangaroo`
  moves work from GPU to CPU. Convenient, but it is the same shape as the bug in
  §5 — recommendation is to report `Blocked` and let a human decide.
- **Whether `state_growth` deserves a general abstraction** when only the
  kangaroo family uses it today.
