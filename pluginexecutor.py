"""Execute Naemon-compatible plugins on a fixed schedule."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

import requests
import yaml


STATUS_NAMES = ("ok", "warning", "critical", "unknown", "out-of-bounds")
OUTPUT_POLICIES = frozenset({"always", "state-change", "non-ok", "never"})
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
PERFDATA_RE = re.compile(
    r"^(?P<label>'[^']+'|[^=\s]+)="
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?P<uom>[^;\s]*)"
    r"(?:;(?P<warn>[^;]*))?"
    r"(?:;(?P<crit>[^;]*))?"
    r"(?:;(?P<minimum>[^;]*))?"
    r"(?:;(?P<maximum>[^;]*))?$"
)


@dataclass(frozen=True)
class TLSOptions:
    """TLS settings for outbound HTTP requests."""

    verify: bool = True
    ca_file: Optional[str] = None
    cert_file: Optional[str] = None
    key_file: Optional[str] = None


@dataclass(frozen=True)
class EndpointConfig:
    """Settings for an optional HTTP integration."""

    enabled: bool = False
    url: Optional[str] = None
    tls_options: TLSOptions = field(default_factory=TLSOptions)

    def requests_kwargs(self) -> dict[str, Any]:
        """Return keyword arguments for requests based on TLS settings."""

        verify: bool | str = self.tls_options.verify
        if verify and self.tls_options.ca_file:
            verify = self.tls_options.ca_file

        kwargs: dict[str, Any] = {"verify": verify}
        if self.tls_options.cert_file and self.tls_options.key_file:
            kwargs["cert"] = (self.tls_options.cert_file, self.tls_options.key_file)
        elif self.tls_options.cert_file:
            kwargs["cert"] = self.tls_options.cert_file
        return kwargs


@dataclass(frozen=True)
class CheckConfig:
    """Runtime settings for a single plugin check."""

    host: str
    service: str
    command: list[str]
    check_period: float
    timeout: float = 60.0
    notification_delay: float = 0.0
    process_perf_data: bool = True
    output: str = "state-change"


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration."""

    checks: list[CheckConfig]
    metrics: EndpointConfig = field(default_factory=EndpointConfig)
    alertmanager: EndpointConfig = field(default_factory=EndpointConfig)


@dataclass
class PerfDatum:
    """Parsed Nagios perfdata for a single metric."""

    label: str
    value: float
    uom: str = ""
    warn: Optional[float] = None
    crit: Optional[float] = None


@dataclass
class CheckState:
    """Mutable in-memory state for a check worker."""

    execution_count: int = 0
    last_status: Optional[str] = None
    last_output: str = ""
    failing_since: Optional[datetime] = None
    alert_active: bool = False
    alert_status: Optional[str] = None
    alert_starts_at: Optional[datetime] = None


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single plugin execution."""

    status: str
    exit_code: Optional[int]
    duration: float
    stdout: str
    stderr: str
    output_text: str
    perfdata: list[PerfDatum]
    finished_at: datetime


def load_config(path: str | Path) -> AppConfig:
    """Load and validate the YAML configuration file."""

    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("top-level config must be a mapping")

    checks_value = raw.get("checks")
    if not isinstance(checks_value, list) or not checks_value:
        raise ValueError("checks must be a non-empty list")

    checks = [parse_check_config(item, index) for index, item in enumerate(checks_value)]
    metrics = parse_endpoint_config(raw.get("metrics"), "metrics")
    alertmanager = parse_endpoint_config(raw.get("alertmanager"), "alertmanager")
    return AppConfig(checks=checks, metrics=metrics, alertmanager=alertmanager)


def parse_check_config(raw: Any, index: int) -> CheckConfig:
    """Convert a single raw check mapping into a validated config object."""

    if not isinstance(raw, dict):
        raise ValueError(f"checks[{index}] must be a mapping")

    host = require_non_empty_string(raw.get("host"), f"checks[{index}].host")
    service = require_non_empty_string(raw.get("service"), f"checks[{index}].service")
    command = parse_command(raw.get("command"), f"checks[{index}].command")
    check_period = require_positive_number(raw.get("check_period"), f"checks[{index}].check_period")
    timeout = require_positive_number(raw.get("timeout", 60), f"checks[{index}].timeout")
    notification_delay = require_non_negative_number(
        raw.get("notification_delay", 0), f"checks[{index}].notification_delay"
    )
    process_perf_data = require_bool(raw.get("process_perf_data", True), f"checks[{index}].process_perf_data")
    output = require_non_empty_string(raw.get("output", "state-change"), f"checks[{index}].output")

    if output not in OUTPUT_POLICIES:
        allowed = ", ".join(sorted(OUTPUT_POLICIES))
        raise ValueError(f"checks[{index}].output must be one of: {allowed}")

    return CheckConfig(
        host=host,
        service=service,
        command=command,
        check_period=check_period,
        timeout=timeout,
        notification_delay=notification_delay,
        process_perf_data=process_perf_data,
        output=output,
    )


def parse_endpoint_config(raw: Any, field_name: str) -> EndpointConfig:
    """Parse config for VictoriaMetrics or Alertmanager delivery."""

    if raw is None:
        return EndpointConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name} must be a mapping")

    enabled = require_bool(raw.get("enabled", False), f"{field_name}.enabled")
    url_value = raw.get("url")
    url = None if url_value is None else require_non_empty_string(url_value, f"{field_name}.url")
    if enabled and not url:
        raise ValueError(f"{field_name}.url is required when {field_name}.enabled is true")

    tls_options = parse_tls_options(raw.get("tls_options"), field_name)
    return EndpointConfig(enabled=enabled, url=url, tls_options=tls_options)


def parse_tls_options(raw: Any, field_name: str) -> TLSOptions:
    """Parse TLS settings for HTTP integrations."""

    if raw is None:
        return TLSOptions()
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name}.tls_options must be a mapping")

    verify = require_bool(raw.get("verify", True), f"{field_name}.tls_options.verify")
    ca_file = optional_string(raw.get("ca_file"), f"{field_name}.tls_options.ca_file")
    cert_file = optional_string(raw.get("cert_file"), f"{field_name}.tls_options.cert_file")
    key_file = optional_string(raw.get("key_file"), f"{field_name}.tls_options.key_file")
    if key_file and not cert_file:
        raise ValueError(f"{field_name}.tls_options.key_file requires cert_file")

    return TLSOptions(verify=verify, ca_file=ca_file, cert_file=cert_file, key_file=key_file)


def parse_command(value: Any, field_name: str) -> list[str]:
    """Validate a plugin command argv list."""

    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    command: list[str] = []
    for index, item in enumerate(value):
        command.append(require_non_empty_string(item, f"{field_name}[{index}]"))
    return command


def require_non_empty_string(value: Any, field_name: str) -> str:
    """Ensure a field is a non-empty string."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def optional_string(value: Any, field_name: str) -> Optional[str]:
    """Return a stripped optional string or validate its absence."""

    if value is None:
        return None
    return require_non_empty_string(value, field_name)


def require_positive_number(value: Any, field_name: str) -> float:
    """Ensure a field is a positive integer or float."""

    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive number")
    return float(value)


def require_non_negative_number(value: Any, field_name: str) -> float:
    """Ensure a field is a non-negative integer or float."""

    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative number")
    return float(value)


def require_bool(value: Any, field_name: str) -> bool:
    """Ensure a field is a boolean."""

    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def map_exit_code(exit_code: Optional[int]) -> str:
    """Translate plugin exit codes into check states."""

    if exit_code is None:
        return "unknown"
    if exit_code == 0:
        return "ok"
    if exit_code == 1:
        return "warning"
    if exit_code == 2:
        return "critical"
    if exit_code == 3:
        return "unknown"
    return "out-of-bounds"


def normalize_text(value: str | bytes | None) -> str:
    """Normalize subprocess output into UTF-8 text."""

    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def execute_check(check: CheckConfig) -> CheckResult:
    """Run a single plugin check and capture its result."""

    started = time.monotonic()
    try:
        completed = subprocess.run(
            check.command,
            capture_output=True,
            text=True,
            timeout=check.timeout,
            check=False,
            shell=False,
        )
        stdout = normalize_text(completed.stdout)
        stderr = normalize_text(completed.stderr)
        output_text, perfdata = parse_plugin_stdout(stdout)
        if not output_text:
            output_text = stderr.strip()
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = normalize_text(exc.stdout)
        stderr = normalize_text(exc.stderr)
        timeout_message = f"timed out after {check.timeout:g} seconds"
        stderr = f"{stderr.strip()} {timeout_message}".strip()
        output_text, perfdata = parse_plugin_stdout(stdout)
        if not output_text:
            output_text = timeout_message
        exit_code = None
    except OSError as exc:
        stdout = ""
        stderr = str(exc)
        output_text = stderr
        perfdata = []
        exit_code = None

    finished_at = datetime.now(timezone.utc)
    duration = time.monotonic() - started
    return CheckResult(
        status=map_exit_code(exit_code),
        exit_code=exit_code,
        duration=duration,
        stdout=stdout,
        stderr=stderr,
        output_text=output_text,
        perfdata=perfdata,
        finished_at=finished_at,
    )


def parse_plugin_stdout(stdout: str) -> tuple[str, list[PerfDatum]]:
    """Split plugin output into human text and parsed perfdata."""

    if not stdout:
        return "", []

    lines = stdout.strip().splitlines()
    if not lines:
        return "", []

    first_line = lines[0]
    message = first_line
    perfdata_text = ""
    if "|" in first_line:
        message, perfdata_text = first_line.split("|", 1)
    extra_output = [line.strip() for line in lines[1:] if line.strip()]
    parts = [message.strip()] + extra_output
    output_text = " ; ".join(part for part in parts if part)
    perfdata = parse_perfdata(perfdata_text)
    return output_text, perfdata


def parse_perfdata(perfdata_text: str) -> list[PerfDatum]:
    """Parse a Nagios perfdata segment into structured values."""

    perfdata: list[PerfDatum] = []
    for token in split_perfdata_tokens(perfdata_text):
        match = PERFDATA_RE.match(token)
        if not match:
            continue
        label = match.group("label")
        if label.startswith("'") and label.endswith("'"):
            label = label[1:-1]
        perfdata.append(
            PerfDatum(
                label=label,
                value=float(match.group("value")),
                uom=match.group("uom") or "",
                warn=parse_threshold(match.group("warn")),
                crit=parse_threshold(match.group("crit")),
            )
        )
    return perfdata


def split_perfdata_tokens(perfdata_text: str) -> list[str]:
    """Split perfdata on whitespace while preserving quoted labels."""

    tokens: list[str] = []
    current: list[str] = []
    in_quote = False
    for char in perfdata_text.strip():
        if char == "'":
            in_quote = not in_quote
            current.append(char)
            continue
        if char.isspace() and not in_quote:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def parse_threshold(value: Optional[str]) -> Optional[float]:
    """Parse simple numeric warn/crit thresholds when available."""

    if not value:
        return None
    stripped = value.strip()
    if NUMERIC_RE.match(stripped):
        return float(stripped)
    return None


def should_log_output(policy: str, previous_status: Optional[str], current_status: str) -> bool:
    """Decide whether to emit stdout for a check result."""

    if policy == "always":
        return True
    if policy == "state-change":
        return previous_status != current_status
    if policy == "non-ok":
        return current_status != "ok"
    if policy == "never":
        return False
    raise ValueError(f"unknown output policy: {policy}")


def build_log_line(check: CheckConfig, result: CheckResult) -> str:
    """Build a single structured log line for stdout."""

    exit_code = "none" if result.exit_code is None else str(result.exit_code)
    return (
        f"timestamp={result.finished_at.isoformat()} "
        f"host={json.dumps(check.host)} "
        f"service={json.dumps(check.service)} "
        f"status={result.status} "
        f"exit_code={exit_code} "
        f"duration={result.duration:.3f} "
        f"stdout={json.dumps(result.stdout.strip())} "
        f"stderr={json.dumps(result.stderr.strip())}"
    )


def emit_internal_log(message: str, stream: Any = None) -> None:
    """Write internal executor messages to stdout."""

    target = stream or sys.stdout
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"timestamp={timestamp} component=pluginexecutor message={json.dumps(message)}", file=target, flush=True)


class VictoriaMetricsClient:
    """Send check results to VictoriaMetrics JSON-line import."""

    def __init__(self, config: EndpointConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def send_result(self, check: CheckConfig, state: CheckState, result: CheckResult) -> None:
        """Build and POST metrics for a check result."""

        if not self.config.enabled or not self.config.url:
            return

        lines = self.build_lines(check, state, result)
        payload = "\n".join(lines)
        response = self.session.post(
            self.config.url,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
            timeout=30,
            **self.config.requests_kwargs(),
        )
        response.raise_for_status()

    @staticmethod
    def build_lines(check: CheckConfig, state: CheckState, result: CheckResult) -> list[str]:
        """Return VictoriaMetrics JSON-line samples for one check execution."""

        timestamp_ms = int(result.finished_at.timestamp() * 1000)
        base_labels = {"host": check.host, "service": check.service}
        lines = [
            build_metric_line("check_executions_total", base_labels, state.execution_count, timestamp_ms),
            build_metric_line("check_duration", base_labels, result.duration, timestamp_ms),
        ]

        for status_name in STATUS_NAMES:
            lines.append(
                build_metric_line(
                    "check_status",
                    {**base_labels, "status": status_name},
                    1 if status_name == result.status else 0,
                    timestamp_ms,
                )
            )

        if check.process_perf_data:
            for datum in result.perfdata:
                labels = {
                    **base_labels,
                    "perf_label": datum.label,
                    "uom": datum.uom,
                }
                lines.append(build_metric_line("check_perf_value", labels, datum.value, timestamp_ms))
                if datum.warn is not None:
                    lines.append(build_metric_line("check_perf_warn", labels, datum.warn, timestamp_ms))
                if datum.crit is not None:
                    lines.append(build_metric_line("check_perf_crit", labels, datum.crit, timestamp_ms))

        return lines


def build_metric_line(name: str, labels: dict[str, str], value: float | int, timestamp_ms: int) -> str:
    """Encode a single VictoriaMetrics JSON-line sample."""

    return json.dumps(
        {
            "metric": {"__name__": name, **labels},
            "values": [value],
            "timestamps": [timestamp_ms],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


class AlertmanagerClient:
    """Send firing and resolved alerts to Alertmanager."""

    def __init__(self, config: EndpointConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def send_alerts(self, alerts: Sequence[dict[str, Any]]) -> None:
        """POST alerts to Alertmanager if enabled."""

        if not alerts or not self.config.enabled or not self.config.url:
            return

        response = self.session.post(
            self.endpoint_url(),
            json=list(alerts),
            timeout=30,
            **self.config.requests_kwargs(),
        )
        response.raise_for_status()

    def endpoint_url(self) -> str:
        """Return the Alertmanager v2 alerts endpoint."""

        if not self.config.url:
            raise ValueError("alertmanager url is not configured")
        if self.config.url.endswith("/api/v2/alerts"):
            return self.config.url
        return self.config.url.rstrip("/") + "/api/v2/alerts"

    @staticmethod
    def build_alert(
        check: CheckConfig,
        status: str,
        output_text: str,
        starts_at: datetime,
        ends_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Build a single Alertmanager alert payload."""

        payload: dict[str, Any] = {
            "labels": {
                "alertname": "PluginCheckFailed",
                "host": check.host,
                "service": check.service,
                "status": status,
            },
            "annotations": {
                "checkoutput": output_text,
            },
            "startsAt": starts_at.isoformat(),
        }
        if ends_at is not None:
            payload["endsAt"] = ends_at.isoformat()
        return payload


class PluginExecutor:
    """Coordinate scheduled check execution and side effects."""

    def __init__(
        self,
        config: AppConfig,
        metrics_client: Optional[VictoriaMetricsClient] = None,
        alertmanager_client: Optional[AlertmanagerClient] = None,
        output_stream: Any = None,
    ) -> None:
        self.config = config
        self.metrics_client = metrics_client or VictoriaMetricsClient(config.metrics)
        self.alertmanager_client = alertmanager_client or AlertmanagerClient(config.alertmanager)
        self.output_stream = output_stream or sys.stdout
        self.stop_event = threading.Event()
        self.states = [CheckState() for _ in config.checks]

    def run(self) -> None:
        """Start all worker threads and block until stopped."""

        threads = []
        for index, check in enumerate(self.config.checks):
            thread = threading.Thread(
                target=self._run_check_loop,
                args=(check, self.states[index]),
                name=f"check-{index}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

        try:
            while any(thread.is_alive() for thread in threads):
                for thread in threads:
                    thread.join(timeout=0.5)
        finally:
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=1)

    def stop(self) -> None:
        """Request all workers to stop."""

        self.stop_event.set()

    def _run_check_loop(self, check: CheckConfig, state: CheckState) -> None:
        """Run one check on a fixed cadence without overlap."""

        next_run = time.monotonic()
        while not self.stop_event.is_set():
            remaining = next_run - time.monotonic()
            if remaining > 0 and self.stop_event.wait(remaining):
                break

            self.run_once(check, state)
            next_run += check.check_period
            while next_run <= time.monotonic():
                next_run += check.check_period

    def run_once(self, check: CheckConfig, state: CheckState) -> CheckResult:
        """Execute one check and process logging, metrics, and alerts."""

        result = execute_check(check)
        state.execution_count += 1

        if should_log_output(check.output, state.last_status, result.status):
            print(build_log_line(check, result), file=self.output_stream, flush=True)

        try:
            self.metrics_client.send_result(check, state, result)
        except requests.RequestException as exc:
            emit_internal_log(
                f"metrics delivery failed for {check.host}/{check.service}: {exc}",
                stream=self.output_stream,
            )

        alerts = self.update_alert_state(check, state, result)
        if alerts:
            try:
                self.alertmanager_client.send_alerts(alerts)
            except requests.RequestException as exc:
                emit_internal_log(
                    f"alert delivery failed for {check.host}/{check.service}: {exc}",
                    stream=self.output_stream,
                )

        state.last_status = result.status
        state.last_output = result.output_text
        return result

    def update_alert_state(self, check: CheckConfig, state: CheckState, result: CheckResult) -> list[dict[str, Any]]:
        """Update in-memory alert state and build any Alertmanager payloads."""

        if not self.config.alertmanager.enabled:
            if result.status == "ok":
                state.failing_since = None
                state.alert_active = False
                state.alert_status = None
                state.alert_starts_at = None
            elif state.failing_since is None:
                state.failing_since = result.finished_at
            return []

        alerts: list[dict[str, Any]] = []
        if result.status == "ok":
            state.failing_since = None
            if state.alert_active and state.alert_status:
                alerts.append(
                    AlertmanagerClient.build_alert(
                        check,
                        state.alert_status,
                        result.output_text,
                        state.alert_starts_at or result.finished_at,
                        ends_at=result.finished_at,
                    )
                )
            state.alert_active = False
            state.alert_status = None
            state.alert_starts_at = None
            return alerts

        if state.failing_since is None:
            state.failing_since = result.finished_at

        if state.alert_active and state.alert_status and state.alert_status != result.status:
            alerts.append(
                AlertmanagerClient.build_alert(
                    check,
                    state.alert_status,
                    result.output_text,
                    state.alert_starts_at or state.failing_since,
                    ends_at=result.finished_at,
                )
            )
            state.alert_active = False
            state.alert_status = None
            state.alert_starts_at = None

        if not state.alert_active:
            failing_for = (result.finished_at - state.failing_since).total_seconds()
            if failing_for >= check.notification_delay:
                state.alert_active = True
                state.alert_status = result.status
                state.alert_starts_at = state.failing_since
                alerts.append(
                    AlertmanagerClient.build_alert(
                        check,
                        result.status,
                        result.output_text,
                        state.alert_starts_at,
                    )
                )

        return alerts


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="path to YAML config file")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the executor until interrupted."""

    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    executor = PluginExecutor(config)

    try:
        import signal

        signal.signal(signal.SIGINT, lambda _signum, _frame: executor.stop())
        signal.signal(signal.SIGTERM, lambda _signum, _frame: executor.stop())
    except ValueError:
        pass

    executor.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
