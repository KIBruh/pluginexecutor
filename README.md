# pluginexecutor

`pluginexecutor` is a small Python daemon for running a few Naemon-compatible plugins from YAML.

It executes checks on a schedule, writes result lines to stdout, sends check metrics to VictoriaMetrics JSON-line import, and sends delayed alerts to Alertmanager.

## Install

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

For tests:

```bash
pip install -e .[dev]
```

## Config

```yaml
checks:
- host: localhost
  service: disk-root
  command:
  - /usr/lib/nagios/plugins/check_disk
  - -w
  - 20%
  - -c
  - 10%
  - -p
  - /
  check_period: 120
  timeout: 60
  notification_delay: 300
  process_perf_data: true
  output: state-change

metrics:
  enabled: true
  url: https://victoriametrics.example/api/v1/import
  tls_options:
    verify: true

alertmanager:
  enabled: true
  url: https://alertmanager.example
  tls_options:
    verify: true
```

## Run

```bash
python pluginexecutor.py /path/to/config.yaml
```

Or, after `pip install -e .`:

```bash
pluginexecutor /path/to/config.yaml
```

## Check Behavior

Exit codes map to states as follows:

- `0` => `ok`
- `1` => `warning`
- `2` => `critical`
- `3` => `unknown`
- `>3` => `out-of-bounds`
- timeout or process start failure => `unknown`

Each check runs immediately at startup and then repeats every `check_period` seconds.

## Output Policy

Allowed `output` values:

- `always`
- `state-change`
- `non-ok`
- `never`

## Metrics

When `metrics.enabled` is true, the executor POSTs newline-delimited JSON objects to the configured VictoriaMetrics import URL.

Per run it emits:

- `check_executions_total{host,service}`
- `check_status{status,host,service}` for each supported status
- `check_duration{host,service}`
- perfdata metrics when perfdata exists and `process_perf_data` is true

## Alerts

When `alertmanager.enabled` is true, the executor sends alerts to `POST /api/v2/alerts`.

If a check stays non-`ok` for at least `notification_delay`, a firing alert is sent with these labels:

- `alertname=PluginCheckFailed`
- `host`
- `service`
- `status`

The annotation `checkoutput` contains the plugin output.

When the check returns to `ok`, a resolved alert is sent.
