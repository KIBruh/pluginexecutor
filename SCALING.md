# Scaling Notes

## Current Concurrency Model

The executor uses a single scheduler loop + a bounded `ThreadPoolExecutor`.

- The scheduler loop iterates all checks on a 100 ms cycle and submits due checks to the pool.
- Each check has an `in_flight` guard and a next-run anchor, so the same check never overlaps with itself.
- A configurable `max_workers` (default 10) limits how many checks can execute simultaneously.
- Checks for different items run independently in the pool.

Implications:

- one expanded check = one scheduling slot + pool task when due
- checks do not block each other (subject to pool capacity)
- runs for the same check do not overlap
- grouped `targets` count after expansion, so a grouped definition can still consume many pool slots

## What This Likely Handles Well

This design should work well for dozens to low hundreds of checks, not thousands.

Rough guidance:

- 10-50 checks: very likely fine
- 50-200 checks: probably fine if checks are short and periods are not very small
- 200-500 checks: depends heavily on check runtime, check period, and HTTP latency
- 1000+ checks: likely needs redesign

## More Important Than Raw Check Count

The main sizing factor is effective concurrency.

Approximation:

```text
average concurrent check executions ~= sum(check_duration / check_period)
```

Examples:

- 100 checks, 1s runtime, 60s period -> about 1.7 concurrent executions
- 300 checks, 5s runtime, 60s period -> about 25 concurrent executions
- 500 checks, 10s runtime, 30s period -> about 167 concurrent executions

The last case is likely too heavy for the current model.

## Current Bottlenecks

The current implementation does:

- one scheduler thread
- one pool task per check execution
- one subprocess per execution
- synchronous HTTP for metrics delivery
- synchronous HTTP for alert delivery

That means scale is limited by:

- pool capacity
- number of concurrent subprocesses
- plugin runtime
- remote HTTP latency
- CPU and memory overhead from the scheduler loop

## Recommendation

Use the current implementation for:

- small setups
- simple plugin fleets
- low to moderate check counts

Benchmark carefully if you expect more than about 100-300 checks.

If larger scale is needed, the next step should be a redesign to:

- separate scheduling from execution further
- queue metrics and alert delivery
- apply backpressure and concurrency limits
