from __future__ import annotations

import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Optional

import requests

from ._alerting import AlertmanagerClient, render_alert_annotations
from ._logging import build_log_line, emit_internal_log, should_log_output
from ._metrics import VictoriaMetricsClient
from ._plugin import compute_check_interval, execute_check
from ._types import AppConfig, CheckConfig, CheckResult, CheckState


class PluginExecutor:
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
        self._lock = threading.Lock()
        self._next_run_times = [
            time.monotonic() + random.uniform(0, min(c.check_period, 60.0))
            for c in config.checks
        ]
        self._in_flight = [False] * len(config.checks)
        self._pool: Optional[ThreadPoolExecutor] = None

    def run(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=self.config.max_workers)
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                sleep_time = 1.0
                with self._lock:
                    next_due: Optional[float] = None
                    for idx, check in enumerate(self.config.checks):
                        if self._in_flight[idx]:
                            continue
                        if now >= self._next_run_times[idx]:
                            self._in_flight[idx] = True
                            self._pool.submit(
                                self._run_and_advance,
                                check,
                                self.states[idx],
                                idx,
                            )
                            continue
                        if next_due is None or self._next_run_times[idx] < next_due:
                            next_due = self._next_run_times[idx]
                    if next_due is not None:
                        sleep_time = max(0.0, next_due - time.monotonic())
                self.stop_event.wait(sleep_time)
        finally:
            self._pool.shutdown(wait=True)

    def stop(self) -> None:
        self.stop_event.set()
        pool = self._pool
        if pool is not None:
            pool.shutdown(wait=True)

    def _advance_schedule(self, idx: int, anchor: float) -> None:
        next_time = anchor + compute_check_interval(
            self.config.checks[idx].check_period
        )
        while next_time <= time.monotonic():
            next_time += compute_check_interval(
                self.config.checks[idx].check_period
            )
        self._next_run_times[idx] = next_time

    def _run_and_advance(
        self, check: CheckConfig, state: CheckState, idx: int
    ) -> None:
        try:
            self.run_once(check, state)
        finally:
            with self._lock:
                self._in_flight[idx] = False
                self._advance_schedule(idx, self._next_run_times[idx])

    def run_once(self, check: CheckConfig, state: CheckState) -> CheckResult:
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
        if not self.config.alertmanager.enabled:
            if result.status == "ok":
                self._reset_alert_state(state)
            elif state.failing_since is None:
                state.failing_since = result.finished_at
            return []

        alerts: list[dict[str, Any]] = []
        if result.status == "ok":
            self._resolve_last_alert(check, state, result, alerts)
            self._reset_alert_state(state)
            return alerts

        if state.failing_since is None:
            state.failing_since = result.finished_at

        failing_for = (result.finished_at - state.failing_since).total_seconds()
        if failing_for >= check.notification_delay:
            self._resolve_last_alert(check, state, result, alerts)
            alerts.append(
                self._build_alert(
                    check, state, result, result.status, result.finished_at,
                )
            )
            state.last_alerted_status = result.status

        return alerts

    def _reset_alert_state(self, state: CheckState) -> None:
        state.failing_since = None
        state.last_alerted_status = None

    def _build_alert(
        self,
        check: CheckConfig,
        state: CheckState,
        result: CheckResult,
        status: str,
        starts_at: datetime,
        *,
        ends_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        return AlertmanagerClient.build_alert(
            check,
            status,
            render_alert_annotations(
                check, result,
                previous_status=state.last_status,
                alert_status=status,
            ),
            starts_at,
            ends_at=ends_at,
        )

    def _resolve_last_alert(
        self,
        check: CheckConfig,
        state: CheckState,
        result: CheckResult,
        alerts: list[dict[str, Any]],
    ) -> None:
        if state.last_alerted_status is None:
            return
        alerts.append(
            self._build_alert(
                check, state, result, state.last_alerted_status,
                state.failing_since or result.finished_at,
                ends_at=result.finished_at,
            )
        )
