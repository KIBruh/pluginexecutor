"""Execute Naemon-compatible plugins on a fixed schedule."""

from __future__ import annotations

from ._alerting import AlertmanagerClient
from ._cli import main, parse_args
from ._config import load_config
from ._executor import PluginExecutor
from ._metrics import VictoriaMetricsClient
from ._loki import LokiClient
from ._types import (
    AppConfig,
    CheckConfig,
    CheckResult,
    CheckState,
    EndpointConfig,
    PerfDatum,
    TLSOptions,
    WebConfig,
)

__all__ = [
    "AlertmanagerClient",
    "AppConfig",
    "CheckConfig",
    "CheckResult",
    "CheckState",
    "EndpointConfig",
    "PerfDatum",
    "PluginExecutor",
    "TLSOptions",
    "VictoriaMetricsClient",
    "LokiClient",
    "WebConfig",
    "load_config",
    "main",
    "parse_args",
]
