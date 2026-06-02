from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional, Sequence

import requests

from ._templating import render_template
from ._types import CheckConfig, CheckResult, EndpointConfig


class AlertmanagerClient:
    def __init__(self, config: EndpointConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.session = session or requests.Session()

    def send_alerts(self, alerts: Sequence[dict[str, Any]]) -> None:
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


def render_alert_annotations(
    check: CheckConfig,
    result: CheckResult,
    previous_status: Optional[str],
    alert_status: str,
) -> dict[str, str]:
    context = {
        **check.template_context,
        "env": os.environ,
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
