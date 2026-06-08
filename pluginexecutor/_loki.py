from __future__ import annotations

from typing import Any, Optional

import requests

from ._metrics import build_metric_labels
from ._types import CheckConfig, CheckResult, EndpointConfig


class LokiClient:
    """Client for pushing log lines to Grafana Loki using JSON format.

    Sends a single stream per check execution with one value entry containing:
    - timestamp in nanoseconds as a string
    - the check output text
    - structured metadata (flat string map)
    """

    def __init__(self, config: EndpointConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def send_result(self, check: CheckConfig, state, result: CheckResult) -> None:
        if not self.config.enabled or not self.config.url:
            return

        payload = self.build_payload(check, result, self.config)
        response = self.session.post(
            self.config.url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
            **self.config.requests_kwargs(),
        )
        response.raise_for_status()

    @staticmethod
    def build_payload(
        check: CheckConfig, result: CheckResult, config: Optional[EndpointConfig]
    ) -> dict[str, Any]:
        # Merge endpoint and check labels; add host and service afterwards
        labels = build_metric_labels(config, check)
        stream_labels = {**labels, "host": check.host, "service": check.service}

        ts_ns = str(int(result.finished_at.timestamp() * 1_000_000_000))
        duration_text = f"{result.duration:.6f}".rstrip("0").rstrip(".")
        metadata = {
            "status": result.status,
            "exit_code": "" if result.exit_code is None else str(result.exit_code),
            "duration_seconds": duration_text,
        }

        return {
            "streams": [
                {
                    "stream": stream_labels,
                    "values": [
                        [ts_ns, result.stdout, metadata],
                    ],
                }
            ]
        }
