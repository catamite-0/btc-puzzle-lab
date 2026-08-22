# Autopilot: current planning boundary

BTC Puzzle Lab currently exposes an autopilot planning preview. It does not yet
provide managed execution. This page describes the behavior that is wired into
the ordinary CLI today and the rules for extending it later.

For the existing single-target execution path, use [AUTO.md](AUTO.md).

## Available commands

```bash
btc-puzzle-lab auto --plan
btc-puzzle-lab auto <id> --plan
```

`--plan-only` remains an alias for `--plan` in both forms.

| Command | Current behavior |
|---|---|
| `auto --plan` | Rank algorithmically selectable live targets from the complete package catalog, then inspect a bounded chain prefix until one target is selected or the result becomes inconclusive |
| `auto <id> --plan` | Bind one catalog target and explain its chain state, host compatibility, algorithm choice, and blockers |

Both commands inspect the CPU, memory, and GPU resources visible to the current
process. They return a detached planning report, not an executable job.

## Selection behavior

The catalog-wide preview orders selectable targets using a versioned,
low-confidence estimate of full-solution time. It checks chain state in that
order:

- an empty or unconfirmed target advances to the next candidate;
- the first confirmed funded target is selected;
- unknown chain state stops the preview as inconclusive; and
- entries after that stopping point are reported as unchecked.

The pinned preview evaluates only the requested puzzle. For a live target it
collects fresh public-chain evidence. A solved practice fixture may bypass that
lookup only after its published solution has been verified against the catalog
address.

A selection means only that the target and algorithm passed the planner's
current checks. The estimate is not calibrated to this machine, is not an
economic optimum, and does not prove that an engine can be installed, built, or
run successfully.

## Read-only contract

Planning may read the package catalog, local host facts, and public chain data.
It does not:

- write `config/`, `data/`, `state/`, `vendor/`, or `bin/`;
- install dependencies, clone sources, compile, or run a solver;
- create a job board or execution state;
- sign, notify, transfer, or broadcast; or
- read, emit, or persist private keys or signed transactions.

Configuration and execution flags such as `--dest`, `--notify`, `--relay`,
`--live`, `--engine`, and runtime tuning overrides are rejected with `--plan`.
This prevents a read-only request from silently changing configuration or
appearing to authorize later work.

The command reports one of three practical outcomes:

| Outcome | Meaning | Exit code |
|---|---|---:|
| selected | A target and algorithm were selected for explanation | 0 |
| no selection | The pinned target was blocked, or no confirmed selectable catalog target was found | 3 |
| inconclusive | Required chain evidence was unknown, so ranking could not safely continue | 3 |

Invalid requests and failures to acquire trustworthy planning evidence use exit
code 2. The report contains remedies where the planner can identify one.

## What is not provided

There is no managed execution service behind these reports. In particular, the
planning path has no detached job ownership, durable scheduler, process
supervision, lease management, checkpoint recovery, or automatic resume. It
also has no practice-run shortcut, candidate or hit intake, notification
delivery, transfer worker, or broadcast authority.

`auto <id>` without `--plan` remains the existing v0.8 single-target runner. It
can configure, provision, build, verify, and start the current watch loop as
documented in [AUTO.md](AUTO.md), but it does not turn a planning report into a
managed job. The advanced `plan`, `batch`, `once`, `watch`, `status`, `audit`,
and `transfer` commands also retain their current independent behavior.

## Principles for future work

Future execution work should be documented as available only after it is wired
through a supported user command and tested end to end. Each increment should:

1. keep planning reports non-executable and free of authority;
2. default transfer and broadcast behavior to disabled or dry-run;
3. establish durable ownership and recovery before claiming detached operation;
4. preserve explicit worker/control trust boundaries and secret handling; and
5. expose a small user-facing contract without publishing speculative storage
   layouts or internal implementation names.

Until those conditions are met, planning and execution remain separate product
paths and this document will not promise managed execution.
