# AGENTS.md

## Commands

- Install runtime deps: `pip install -e .`
- Install test deps: `pip install -e .[dev]`
- Run the daemon directly: `python pluginexecutor.py /path/to/config.yaml`
- Run via console script after install: `pluginexecutor /path/to/config.yaml`
- Run all tests: `pytest`
- Run a focused test: `pytest tests/test_pluginexecutor.py -k <pattern>`

## Repo Shape

- The whole app lives in `pluginexecutor.py`. There is no package directory split yet.
- Tests are concentrated in `tests/test_pluginexecutor.py`; if you change parsing, scheduling, metrics, or alert behavior, update that file.
- `pyproject.toml` is minimal: setuptools build, console script entrypoint `pluginexecutor:main`, and pytest configured to use `tests/`.

## Runtime Model

- `load_config()` expands grouped `checks[*].targets` into flat checks before runtime. One grouped check can turn into many actual worker checks.
- `PluginExecutor.run()` starts one thread per expanded check. This is the main scaling constraint.
- Scheduling is fixed-cadence with jitter, not fixed-delay: each next run is advanced by `check_period` minus random jitter of `0 to +1%`, capped at `5s` (so intervals are always ≤ `check_period`).
- Checks for the same expanded item never overlap, but different checks run independently.

## Config And Templating

- Only `service`, `command` and `alert_annotations` are Jinja-templated.
- `service` is rendered before `command` and `alert_annotations`, so the rendered `service` value is available as `{{ service }}` in those templates.
- Jinja uses `StrictUndefined`; missing template variables fail config load immediately.
- Grouped `targets` require every target to have the same key set, and every target must include `host`.
- Default alert annotations are injected when omitted: `checkoutput: "{{ output_text }}"`.

## Metrics And Perfdata

- VictoriaMetrics output is sent as newline-delimited JSON to the configured import URL.
- Plugin perfdata is collected from every `|` segment across multi-line plugin output, not just the first line.
- Scalar `warn` and `crit` perfdata thresholds emit `check_perf_warn` and `check_perf_crit` with `threshold_fill=none`.
- Nagios range thresholds emit bound metrics: `check_perf_warn_min/max` and `check_perf_crit_min/max`, with `threshold_fill=outer` or `inner` for `@`-prefixed ranges.
- Open-ended Nagios ranges emit only the bound that exists; perfdata values of `U` are ignored.
- Numeric `min` and `max` perfdata fields emit `check_perf_min` and `check_perf_max`.

## Scaling

- Read `SCALING.md` before changing the concurrency model. The current design uses a scheduler loop and a bounded `ThreadPoolExecutor` (`max_workers`, default 10). It is intended for dozens to low hundreds of checks, not thousands.
- Because the app uses one pool slot per concurrently executing check and synchronous subprocess/HTTP work, changes that increase pool occupancy or per-check latency have immediate scaling impact.
