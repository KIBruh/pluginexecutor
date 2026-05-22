"""Execute Naemon-compatible plugins on a fixed schedule."""

from __future__ import annotations

import argparse
import json
import random
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
from jinja2 import Environment, StrictUndefined, TemplateError


STATUS_NAMES = ("ok", "warning", "critical", "unknown", "out-of-bounds")
OUTPUT_POLICIES = frozenset({"always", "state-change", "non-ok", "never"})
DEFAULT_ALERT_ANNOTATIONS = {"checkoutput": "{{ output_text }}"}
TEMPLATE_ENVIRONMENT = Environment(autoescape=False, undefined=StrictUndefined)
INTERNAL_TEMPLATE_CONTEXT_KEY = "__template_context"
INTERNAL_ALERT_ANNOTATIONS_KEY = "__alert_annotation_templates"
MAX_SCHEDULING_JITTER_SECONDS = 5.0
SCHEDULING_JITTER_RATIO = 0.01
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
RANGE_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
PERFDATA_RE = re.compile(
    r"^(?P<label>'[^']+'|[^=\s]+)="
    r"(?P<value>U|[+-]?(?:\d+(?:[\.,]\d*)?|[\.,]\d+))"
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
    template_context: dict[str, Any] = field(default_factory=dict)
    alert_annotations: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_ALERT_ANNOTATIONS)
    )


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
    warn_min: Optional[float] = None
    warn_max: Optional[float] = None
    warn_fill: Optional[str] = None
    crit_min: Optional[float] = None
    crit_max: Optional[float] = None
    crit_fill: Optional[str] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None


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

    normalized_checks = normalize_checks(checks_value)
    checks = [parse_check_config(item, index) for index, item in enumerate(normalized_checks)]
    metrics = parse_endpoint_config(raw.get("metrics"), "metrics")
    alertmanager = parse_endpoint_config(raw.get("alertmanager"), "alertmanager")
    return AppConfig(checks=checks, metrics=metrics, alertmanager=alertmanager)


def normalize_checks(raw_checks: list[Any]) -> list[dict[str, Any]]:
    """Expand grouped checks and render load-time templates."""

    checks: list[dict[str, Any]] = []
    for index, raw_check in enumerate(raw_checks):
        field_name = f"checks[{index}]"
        if not isinstance(raw_check, dict):
            raise ValueError(f"{field_name} must be a mapping")

        if "targets" in raw_check:
            checks.extend(expand_grouped_check(raw_check, field_name))
        else:
            checks.append(normalize_single_check(raw_check, field_name))

    return checks


def expand_grouped_check(raw_check: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    """Expand a grouped check with targets into flat check definitions."""

    targets = raw_check.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError(f"{field_name}.targets must be a non-empty list")

    base_check = dict(raw_check)
    base_check.pop("targets", None)

    normalized_targets = [
        normalize_target(target, field_name, index) for index, target in enumerate(targets)
    ]
    expected_keys = set(normalized_targets[0])
    if "host" not in expected_keys:
        raise ValueError(f"{field_name}.targets[0].host must be a non-empty string")

    overlap = expected_keys & set(base_check)
    if overlap:
        keys = ", ".join(sorted(overlap))
        raise ValueError(f"{field_name}.targets keys must not overlap parent keys: {keys}")

    checks: list[dict[str, Any]] = []
    for target_index, target in enumerate(normalized_targets):
        if set(target) != expected_keys:
            keys = ", ".join(sorted(expected_keys))
            raise ValueError(
                f"{field_name}.targets[{target_index}] must contain exactly these keys: {keys}"
            )

        merged = {**base_check, **target}
        checks.append(normalize_single_check(merged, f"{field_name}.targets[{target_index}]"))

    return checks


def normalize_target(raw_target: Any, field_name: str, index: int) -> dict[str, Any]:
    """Validate a target mapping used for grouped check expansion."""

    target_field = f"{field_name}.targets[{index}]"
    if not isinstance(raw_target, dict):
        raise ValueError(f"{target_field} must be a mapping")

    target: dict[str, Any] = {}
    for key, value in raw_target.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{target_field} keys must be non-empty strings")
        target[key] = value

    require_non_empty_string(target.get("host"), f"{target_field}.host")
    return target


def normalize_single_check(raw_check: dict[str, Any], field_name: str) -> dict[str, Any]:
    """Render templates and attach internal metadata for one flat check."""

    context = build_template_context(raw_check)
    normalized_check = dict(raw_check)
    normalized_check["command"] = render_command_templates(
        raw_check.get("command"), context, f"{field_name}.command"
    )
    normalized_check[INTERNAL_TEMPLATE_CONTEXT_KEY] = context
    normalized_check[INTERNAL_ALERT_ANNOTATIONS_KEY] = parse_alert_annotation_templates(
        raw_check.get("alert_annotations"), f"{field_name}.alert_annotations"
    )
    return normalized_check


def build_template_context(raw_check: dict[str, Any]) -> dict[str, Any]:
    """Collect static template variables from a raw check definition."""

    context: dict[str, Any] = {}
    for key, value in raw_check.items():
        if key in {"command", "alert_annotations", "targets"}:
            continue
        context[key] = value
    return context


def render_command_templates(value: Any, context: dict[str, Any], field_name: str) -> list[str]:
    """Render Jinja templates in command arguments."""

    command = parse_command(value, field_name)
    return [
        render_template(argument, context, f"{field_name}[{index}]")
        for index, argument in enumerate(command)
    ]


def parse_alert_annotation_templates(value: Any, field_name: str) -> dict[str, str]:
    """Validate optional alert annotation templates."""

    if value is None:
        return dict(DEFAULT_ALERT_ANNOTATIONS)
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field_name} must be a non-empty mapping")

    annotations: dict[str, str] = {}
    for key, template in value.items():
        annotation_key = require_non_empty_string(key, f"{field_name} key")
        annotations[annotation_key] = require_non_empty_string(
            template, f"{field_name}.{annotation_key}"
        )
    return annotations


def render_template(template: str, context: dict[str, Any], field_name: str) -> str:
    """Render a single Jinja template string with strict variable handling."""

    try:
        return TEMPLATE_ENVIRONMENT.from_string(template).render(context)
    except TemplateError as exc:
        raise ValueError(f"failed to render {field_name}: {exc}") from exc


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
    process_perf_data = require_bool(
        raw.get("process_perf_data", True), f"checks[{index}].process_perf_data"
    )
    output = require_non_empty_string(raw.get("output", "state-change"), f"checks[{index}].output")
    template_context = require_template_context(
        raw.get(INTERNAL_TEMPLATE_CONTEXT_KEY, {}),
        f"checks[{index}].{INTERNAL_TEMPLATE_CONTEXT_KEY}",
    )
    alert_annotations = parse_alert_annotation_templates(
        raw.get(INTERNAL_ALERT_ANNOTATIONS_KEY),
        f"checks[{index}].alert_annotations",
    )

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
        template_context=template_context,
        alert_annotations=alert_annotations,
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


def require_template_context(value: Any, field_name: str) -> dict[str, Any]:
    """Ensure the internal template context is a mapping."""

    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return dict(value)


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


def compute_check_interval(check_period: float) -> float:
    """Return the next scheduling interval with bounded random jitter."""

    jitter = min(MAX_SCHEDULING_JITTER_SECONDS, check_period * SCHEDULING_JITTER_RATIO)
    return max(
        0.0,
        check_period + random.uniform(-jitter, jitter),
    )


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

    output_parts: list[str] = []
    perfdata_parts: list[str] = []
    for line in lines:
        text_part = line
        perfdata_part = ""
        if "|" in line:
            text_part, perfdata_part = line.split("|", 1)
        text_part = text_part.strip()
        if text_part:
            output_parts.append(text_part)
        perfdata_part = perfdata_part.strip()
        if perfdata_part:
            perfdata_parts.append(perfdata_part)

    parts = output_parts
    output_text = " ; ".join(part for part in parts if part)
    perfdata = parse_perfdata(" ".join(perfdata_parts))
    return output_text, perfdata


def parse_perfdata(perfdata_text: str) -> list[PerfDatum]:
    """Parse a Nagios perfdata segment into structured values."""

    perfdata: list[PerfDatum] = []
    for token in split_perfdata_tokens(perfdata_text):
        match = PERFDATA_RE.match(token)
        if not match:
            continue
        value_text = normalize_perf_number(match.group("value"))
        if value_text is None:
            continue
        label = match.group("label")
        if label.startswith("'") and label.endswith("'"):
            label = label[1:-1]
        warn = parse_perf_threshold(match.group("warn"))
        crit = parse_perf_threshold(match.group("crit"))
        perfdata.append(
            PerfDatum(
                label=label,
                value=float(value_text),
                uom=match.group("uom") or "",
                warn=warn["value"],
                crit=crit["value"],
                warn_min=warn["minimum"],
                warn_max=warn["maximum"],
                warn_fill=warn["fill"],
                crit_min=crit["minimum"],
                crit_max=crit["maximum"],
                crit_fill=crit["fill"],
                minimum=parse_numeric_perf_field(match.group("minimum")),
                maximum=parse_numeric_perf_field(match.group("maximum")),
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


def parse_numeric_perf_field(value: Optional[str]) -> Optional[float]:
    """Parse a perfdata field only when it is a plain numeric scalar."""

    if not value:
        return None
    stripped = normalize_perf_number(value)
    if stripped is not None:
        return float(stripped)
    return None


def normalize_perf_number(value: Optional[str]) -> Optional[str]:
    """Normalize a perfdata numeric string to Python float format."""

    if not value:
        return None
    stripped = value.strip().replace(",", ".")
    if stripped == "U":
        return None
    if NUMERIC_RE.match(stripped):
        return stripped
    return None


def parse_perf_threshold(value: Optional[str]) -> dict[str, Optional[float] | Optional[str]]:
    """Parse scalar or range perfdata thresholds into structured bounds."""

    parsed: dict[str, Optional[float] | Optional[str]] = {
        "value": None,
        "minimum": None,
        "maximum": None,
        "fill": None,
    }
    if not value:
        return parsed

    stripped = value.strip().replace(",", ".")
    if not stripped:
        return parsed

    if NUMERIC_RE.match(stripped):
        parsed["value"] = float(stripped)
        parsed["fill"] = "none"
        return parsed

    fill = "outer"
    range_text = stripped
    if range_text.startswith("@"):
        fill = "inner"
        range_text = range_text[1:]

    if ":" not in range_text:
        return parsed

    lower_text, upper_text = range_text.split(":", 1)
    lower = parse_perf_range_bound(lower_text, allow_infinite_low=True)
    upper = parse_perf_range_bound(upper_text, allow_infinite_low=False)
    if lower is None and upper is None:
        return parsed

    parsed["minimum"] = lower
    parsed["maximum"] = upper
    parsed["fill"] = fill
    return parsed


def parse_perf_range_bound(value: str, *, allow_infinite_low: bool) -> Optional[float]:
    """Parse one perfdata range bound, treating `~` as unbounded."""

    stripped = value.strip()
    if not stripped or (allow_infinite_low and stripped == "~"):
        return None
    match = RANGE_NUMBER_RE.search(stripped)
    if not match:
        return None
    return float(match.group(0))


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
    print(
        f"timestamp={timestamp} component=pluginexecutor message={json.dumps(message)}",
        file=target,
        flush=True,
    )


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
            build_metric_line(
                "check_executions_total",
                base_labels,
                state.execution_count,
                timestamp_ms,
            ),
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
                lines.append(
                    build_metric_line("check_perf_value", labels, datum.value, timestamp_ms)
                )
                if datum.warn is not None:
                    lines.append(
                        build_metric_line(
                            "check_perf_warn",
                            {**labels, "threshold_fill": datum.warn_fill or "none"},
                            datum.warn,
                            timestamp_ms,
                        )
                    )
                if datum.crit is not None:
                    lines.append(
                        build_metric_line(
                            "check_perf_crit",
                            {**labels, "threshold_fill": datum.crit_fill or "none"},
                            datum.crit,
                            timestamp_ms,
                        )
                    )
                if datum.warn_min is not None:
                    lines.append(
                        build_metric_line(
                            "check_perf_warn_min",
                            {**labels, "threshold_fill": datum.warn_fill or "outer"},
                            datum.warn_min,
                            timestamp_ms,
                        )
                    )
                if datum.warn_max is not None:
                    lines.append(
                        build_metric_line(
                            "check_perf_warn_max",
                            {**labels, "threshold_fill": datum.warn_fill or "outer"},
                            datum.warn_max,
                            timestamp_ms,
                        )
                    )
                if datum.crit_min is not None:
                    lines.append(
                        build_metric_line(
                            "check_perf_crit_min",
                            {**labels, "threshold_fill": datum.crit_fill or "outer"},
                            datum.crit_min,
                            timestamp_ms,
                        )
                    )
                if datum.crit_max is not None:
                    lines.append(
                        build_metric_line(
                            "check_perf_crit_max",
                            {**labels, "threshold_fill": datum.crit_fill or "outer"},
                            datum.crit_max,
                            timestamp_ms,
                        )
                    )
                if datum.minimum is not None:
                    lines.append(
                        build_metric_line("check_perf_min", labels, datum.minimum, timestamp_ms)
                    )
                if datum.maximum is not None:
                    lines.append(
                        build_metric_line("check_perf_max", labels, datum.maximum, timestamp_ms)
                    )

        return lines


def build_metric_line(
    name: str, labels: dict[str, str], value: float | int, timestamp_ms: int
) -> str:
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
        annotations: dict[str, str],
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
            "annotations": annotations,
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
            next_run += compute_check_interval(check.check_period)
            while next_run <= time.monotonic():
                next_run += compute_check_interval(check.check_period)

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

    def update_alert_state(
        self, check: CheckConfig, state: CheckState, result: CheckResult
    ) -> list[dict[str, Any]]:
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
                        render_alert_annotations(
                            check,
                            result,
                            previous_status=state.last_status,
                            alert_status=state.alert_status,
                        ),
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
                    render_alert_annotations(
                        check,
                        result,
                        previous_status=state.last_status,
                        alert_status=state.alert_status,
                    ),
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
                        render_alert_annotations(
                            check,
                            result,
                            previous_status=state.last_status,
                            alert_status=result.status,
                        ),
                        state.alert_starts_at,
                    )
                )

        return alerts


def render_alert_annotations(
    check: CheckConfig,
    result: CheckResult,
    previous_status: Optional[str],
    alert_status: str,
) -> dict[str, str]:
    """Render per-check alert annotation templates from runtime state."""

    context = {
        **check.template_context,
        "host": check.host,
        "service": check.service,
        "status": alert_status,
        "current_status": result.status,
        "previous_status": previous_status,
        "output_text": result.output_text,
        "exit_code": result.exit_code,
        "duration": result.duration,
        "notification_delay": check.notification_delay,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    annotations: dict[str, str] = {}
    for key, template in check.alert_annotations.items():
        annotations[key] = render_template(template, context, f"alert_annotations.{key}")
    return annotations


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
