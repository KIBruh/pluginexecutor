## Overview

Build a small Python daemon that executes a configured set of Naemon-compatible plugins on a fixed schedule, writes status output to stdout, ships metrics to VictoriaMetrics in JSON-line format, and sends alerts to Alertmanager after a configurable delay.

## Files

- `pyproject.toml`
- `pluginexecutor.py`
- `README.md`
- `tests/test_pluginexecutor.py`

## Dependencies

- `PyYAML` for config parsing
- `requests` for HTTP delivery to VictoriaMetrics and Alertmanager
- `pytest` for tests

## Config Model

Use the following top-level sections:

- `checks`
- `metrics`
- `alertmanager`

Each check contains:

- `host`
- `service`
- `command`
- `check_period`
- `timeout` default `60`
- `notification_delay` default `0`
- `process_perf_data` default `true`
- `output` default `state-change`

Allowed `output` values:

- `always`
- `state-change`
- `non-ok`
- `never`

## Runtime Model

- Load config once at startup.
- Start one thread per check.
- Run each check immediately on startup and then every `check_period` seconds.
- Never overlap executions for the same check.
- Keep runtime state in memory only.

Per-check runtime state includes:

- execution counter
- last status
- last output text
- failing-since timestamp
- whether an alert is active
- active alert status

## Check Execution

Use `subprocess.run()` with:

- explicit argv list
- `shell=False`
- captured stdout/stderr
- configured timeout

Exit code mapping:

- `0` => `ok`
- `1` => `warning`
- `2` => `critical`
- `3` => `unknown`
- `>3` => `out-of-bounds`
- timeout or process start failure => `unknown`

Record for every execution:

- status
- exit code
- duration in seconds
- stdout
- stderr

## Logging

Log to stdout in one line with:

- timestamp
- host
- service
- status
- exit code
- duration
- stdout
- stderr

Apply the `output` policy as follows:

- `always`: log every run
- `state-change`: log only when status changes
- `non-ok`: log only when status is not `ok`
- `never`: suppress routine check logs

## Perfdata

Parse Nagios/Naemon perfdata after `|` in plugin stdout.

Extract:

- perf label
- value
- UOM
- warn threshold
- crit threshold

Malformed perfdata must not fail the check.

## Metrics

When metrics are enabled, POST newline-delimited JSON objects to the configured VictoriaMetrics `/api/v1/import` endpoint.

Each line uses:

- `metric`
- `values`
- `timestamps`

Metric name is stored in `metric.__name__` and timestamps are Unix milliseconds.

Emit on every check run:

- `check_executions_total{host,service}`
- `check_status{status="ok",host,service}`
- `check_status{status="warning",host,service}`
- `check_status{status="critical",host,service}`
- `check_status{status="unknown",host,service}`
- `check_status{status="out-of-bounds",host,service}`
- `check_duration{host,service}`

When `process_perf_data` is enabled and perfdata is present, also emit:

- `check_perf_value{host,service,perf_label,uom}`
- `check_perf_warn{host,service,perf_label,uom}`
- `check_perf_crit{host,service,perf_label,uom}`

Metric delivery failures must be logged and ignored.

## Alertmanager

When Alertmanager is enabled, POST alerts to `/api/v2/alerts`.

Behavior:

- if a check becomes non-`ok`, record `failing_since`
- once it remains non-`ok` for at least `notification_delay`, send a firing alert
- if status changes while the alert is active, send an updated firing alert
- when the check returns to `ok`, send a resolved alert

Labels:

- `alertname=PluginCheckFailed`
- `host`
- `service`
- `status`

Annotations:

- `checkoutput`

## Validation

Fail startup for:

- missing or empty `checks`
- missing `host`, `service`, or `command`
- non-positive `check_period`
- non-positive `timeout`
- negative `notification_delay`
- invalid `output`
- enabled metrics without `metrics.url`
- enabled Alertmanager without `alertmanager.url`

## CLI

Run as:

```bash
python pluginexecutor.py /path/to/config.yaml
```

## Tests

Add tests for:

- config defaults and validation
- exit code mapping
- timeout and execution failure mapping
- perfdata parsing
- output policy decisions
- VictoriaMetrics payload generation
- Alertmanager payload generation
- alert notification delay state handling
