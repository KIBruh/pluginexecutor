"""Execute Naemon-compatible plugins on a fixed schedule."""

from __future__ import annotations

# stdlib imports exposed at package level for test monkeypatch access
import random  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401

from ._alerting import AlertmanagerClient, render_alert_annotations
from ._cli import main, parse_args
from ._config import (
    build_template_context,
    expand_grouped_check,
    load_config,
    normalize_checks,
    normalize_single_check,
    normalize_target,
    optional_string,
    parse_alert_annotation_templates,
    parse_check_config,
    parse_command,
    parse_endpoint_config,
    parse_tls_options,
    render_command_templates,
    render_template,
    require_bool,
    require_non_empty_string,
    require_non_negative_number,
    require_positive_number,
    require_template_context,
)
from ._constants import (
    COMMAND_ARG_DROP_SENTINEL,
    DEFAULT_ALERT_ANNOTATIONS,
    INTERNAL_ALERT_ANNOTATIONS_KEY,
    INTERNAL_TEMPLATE_CONTEXT_KEY,
    MAX_SCHEDULING_JITTER_SECONDS,
    NUMERIC_RE,
    OUTPUT_POLICIES,
    PERFDATA_RE,
    RANGE_NUMBER_RE,
    SCHEDULING_JITTER_RATIO,
    STATUS_NAMES,
    TEMPLATE_ENVIRONMENT,
    read_template_file,
)
from ._executor import PluginExecutor
from ._logging import build_log_line, emit_internal_log, should_log_output
from ._metrics import VictoriaMetricsClient, build_metric_line
from ._plugin import (
    compute_check_interval,
    execute_check,
    map_exit_code,
    normalize_perf_number,
    normalize_text,
    parse_numeric_perf_field,
    parse_perf_range_bound,
    parse_perf_threshold,
    parse_perfdata,
    parse_plugin_stdout,
    split_perfdata_tokens,
)
from ._types import (
    AppConfig,
    CheckConfig,
    CheckResult,
    CheckState,
    EndpointConfig,
    PerfDatum,
    TLSOptions,
)
