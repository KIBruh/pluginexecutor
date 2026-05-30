from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

from ._constants import (
    COMMAND_ARG_DROP_SENTINEL,
    DEFAULT_ALERT_ANNOTATIONS,
    INTERNAL_ALERT_ANNOTATIONS_KEY,
    INTERNAL_TEMPLATE_CONTEXT_KEY,
    OUTPUT_POLICIES,
    TEMPLATE_ENVIRONMENT,
)
from ._types import AppConfig, CheckConfig, EndpointConfig, TLSOptions


def load_config(path: str | Path) -> AppConfig:
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
    max_workers = raw.get("max_workers", 10)
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ValueError("max_workers must be a positive integer")
    return AppConfig(
        checks=checks,
        metrics=metrics,
        alertmanager=alertmanager,
        max_workers=max_workers,
    )


def normalize_checks(raw_checks: list[Any]) -> list[dict[str, Any]]:
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
    context = build_template_context(raw_check)
    normalized_check = dict(raw_check)
    service_raw = normalized_check.pop("service", "")
    service = render_template(service_raw, context, f"{field_name}.service")
    normalized_check["service"] = service
    context["service"] = service
    normalized_check["command"] = render_command_templates(
        raw_check.get("command"), context, f"{field_name}.command"
    )
    normalized_check[INTERNAL_TEMPLATE_CONTEXT_KEY] = context
    normalized_check[INTERNAL_ALERT_ANNOTATIONS_KEY] = parse_alert_annotation_templates(
        raw_check.get("alert_annotations"), f"{field_name}.alert_annotations"
    )
    return normalized_check


def build_template_context(raw_check: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {
        "env": os.environ,
        "drop_arg": COMMAND_ARG_DROP_SENTINEL,
    }
    for key, value in raw_check.items():
        if key in {"command", "alert_annotations", "targets"}:
            continue
        context[key] = value
    return context


def render_command_templates(value: Any, context: dict[str, Any], field_name: str) -> list[str]:
    command = parse_command(value, field_name)
    rendered: list[str] = []
    for index, argument in enumerate(command):
        rendered_argument = render_template(argument, context, f"{field_name}[{index}]")
        if rendered_argument == COMMAND_ARG_DROP_SENTINEL:
            continue
        rendered.append(
            require_non_empty_string(
                rendered_argument,
                f"{field_name}[{index}]",
                strip=False,
            )
        )
    if not rendered:
        raise ValueError(f"{field_name} must contain at least one argument after template rendering")
    return rendered


def parse_alert_annotation_templates(value: Any, field_name: str) -> dict[str, str]:
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
    from jinja2 import TemplateError

    try:
        return TEMPLATE_ENVIRONMENT.from_string(template).render(context)
    except TemplateError as exc:
        raise ValueError(f"failed to render {field_name}: {exc}") from exc


def parse_check_config(raw: Any, index: int) -> CheckConfig:
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
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty list")
    command: list[str] = []
    for index, item in enumerate(value):
        command.append(require_non_empty_string(item, f"{field_name}[{index}]", strip=False))
    return command


def require_template_context(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return dict(value)


def require_non_empty_string(value: Any, field_name: str, *, strip: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip() if strip else value


def optional_string(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return require_non_empty_string(value, field_name)


def require_positive_number(value: Any, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive number")
    return float(value)


def require_non_negative_number(value: Any, field_name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative number")
    return float(value)


def require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value
