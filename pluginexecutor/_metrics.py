from __future__ import annotations

import json
from typing import Optional

import requests

from ._constants import STATUS_NAMES
from ._types import CheckConfig, CheckResult, CheckState, EndpointConfig


class VictoriaMetricsClient:
    def __init__(self, config: EndpointConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def send_result(self, check: CheckConfig, state: CheckState, result: CheckResult) -> None:
        if not self.config.enabled or not self.config.url:
            return

        lines = self.build_lines(check, state, result, self.config)
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
    def build_lines(
        check: CheckConfig,
        state: CheckState,
        result: CheckResult,
        config: Optional[EndpointConfig] = None,
    ) -> list[str]:
        timestamp_ms = int(result.finished_at.timestamp() * 1000)
        metric_labels = build_metric_labels(config, check)
        base_labels = {**metric_labels, "host": check.host, "service": check.service}
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


def build_metric_labels(config: Optional[EndpointConfig], check: CheckConfig) -> dict[str, str]:
    labels: dict[str, str] = {}
    if config is not None:
        labels.update(config.labels)
    labels.update(check.labels)
    return labels


def build_metric_line(
    name: str, labels: dict[str, str], value: float | int, timestamp_ms: int
) -> str:
    return json.dumps(
        {
            "metric": {"__name__": name, **labels},
            "values": [value],
            "timestamps": [timestamp_ms],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
