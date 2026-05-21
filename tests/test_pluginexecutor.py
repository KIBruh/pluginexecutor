from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import pluginexecutor


def make_check(**overrides):
    values = {
        "host": "host-a",
        "service": "service-a",
        "command": ["/bin/true"],
        "check_period": 60.0,
        "timeout": 30.0,
        "notification_delay": 0.0,
        "process_perf_data": True,
        "output": "state-change",
    }
    values.update(overrides)
    return pluginexecutor.CheckConfig(**values)


def make_result(status="ok", output_text="OK", finished_at=None, perfdata=None):
    return pluginexecutor.CheckResult(
        status=status,
        exit_code=0 if status == "ok" else 2,
        duration=1.25,
        stdout=output_text,
        stderr="",
        output_text=output_text,
        perfdata=perfdata or [],
        finished_at=finished_at or datetime(2026, 5, 21, tzinfo=timezone.utc),
    )


def test_load_config_applies_defaults(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- host: localhost
  service: ping
  command:
  - /bin/true
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = pluginexecutor.load_config(config_path)
    check = config.checks[0]
    assert check.timeout == 60.0
    assert check.notification_delay == 0.0
    assert check.process_perf_data is True
    assert check.output == "state-change"
    assert config.metrics.enabled is False
    assert config.alertmanager.enabled is False


def test_load_config_rejects_invalid_output(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- host: localhost
  service: ping
  command:
  - /bin/true
  check_period: 15
  output: noisy
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="output"):
        pluginexecutor.load_config(config_path)


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [
        (0, "ok"),
        (1, "warning"),
        (2, "critical"),
        (3, "unknown"),
        (4, "out-of-bounds"),
        (None, "unknown"),
    ],
)
def test_map_exit_code(exit_code, expected):
    assert pluginexecutor.map_exit_code(exit_code) == expected


def test_parse_perfdata_supports_quoted_labels():
    perfdata = pluginexecutor.parse_perfdata("'queue depth'=4ms;10;20 size=1.5GB;2;4 broken=5%;~:10;20")

    assert perfdata[0] == pluginexecutor.PerfDatum(label="queue depth", value=4.0, uom="ms", warn=10.0, crit=20.0)
    assert perfdata[1] == pluginexecutor.PerfDatum(label="size", value=1.5, uom="GB", warn=2.0, crit=4.0)
    assert perfdata[2].warn is None
    assert perfdata[2].crit == 20.0


def test_should_log_output():
    assert pluginexecutor.should_log_output("always", "ok", "ok") is True
    assert pluginexecutor.should_log_output("state-change", "ok", "warning") is True
    assert pluginexecutor.should_log_output("state-change", "ok", "ok") is False
    assert pluginexecutor.should_log_output("non-ok", "ok", "critical") is True
    assert pluginexecutor.should_log_output("non-ok", "warning", "ok") is False
    assert pluginexecutor.should_log_output("never", None, "critical") is False


def test_build_victoriametrics_lines_include_status_and_perfdata():
    check = make_check()
    state = pluginexecutor.CheckState(execution_count=3)
    result = make_result(
        status="warning",
        perfdata=[pluginexecutor.PerfDatum(label="latency", value=1.5, uom="s", warn=3.0, crit=5.0)],
    )

    lines = pluginexecutor.VictoriaMetricsClient.build_lines(check, state, result)
    payloads = [json.loads(line) for line in lines]

    names = {payload["metric"]["__name__"] for payload in payloads}
    assert "check_executions_total" in names
    assert "check_duration" in names
    assert "check_status" in names
    assert "check_perf_value" in names
    assert "check_perf_warn" in names
    assert "check_perf_crit" in names

    warning_status = [payload for payload in payloads if payload["metric"]["__name__"] == "check_status" and payload["metric"]["status"] == "warning"]
    assert warning_status[0]["values"] == [1]


def test_build_alert_payload():
    check = make_check()
    starts_at = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    ends_at = starts_at + timedelta(minutes=5)

    alert = pluginexecutor.AlertmanagerClient.build_alert(check, "critical", "disk full", starts_at, ends_at)

    assert alert["labels"]["alertname"] == "PluginCheckFailed"
    assert alert["labels"]["host"] == check.host
    assert alert["labels"]["service"] == check.service
    assert alert["labels"]["status"] == "critical"
    assert alert["annotations"]["checkoutput"] == "disk full"
    assert alert["startsAt"] == starts_at.isoformat()
    assert alert["endsAt"] == ends_at.isoformat()


def test_update_alert_state_honors_notification_delay():
    config = pluginexecutor.AppConfig(
        checks=[make_check(notification_delay=30.0)],
        alertmanager=pluginexecutor.EndpointConfig(enabled=True, url="https://alertmanager.example"),
    )
    executor = pluginexecutor.PluginExecutor(config, output_stream=io.StringIO())
    check = config.checks[0]
    state = pluginexecutor.CheckState()
    first_result = make_result(status="critical", finished_at=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc))
    second_result = make_result(status="critical", finished_at=datetime(2026, 5, 21, 10, 0, 31, tzinfo=timezone.utc))

    assert executor.update_alert_state(check, state, first_result) == []
    alerts = executor.update_alert_state(check, state, second_result)

    assert len(alerts) == 1
    assert alerts[0]["labels"]["status"] == "critical"
    assert state.alert_active is True


def test_update_alert_state_resolves_and_refires_on_severity_change():
    config = pluginexecutor.AppConfig(
        checks=[make_check(notification_delay=0.0)],
        alertmanager=pluginexecutor.EndpointConfig(enabled=True, url="https://alertmanager.example"),
    )
    executor = pluginexecutor.PluginExecutor(config, output_stream=io.StringIO())
    check = config.checks[0]
    state = pluginexecutor.CheckState(
        failing_since=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        alert_active=True,
        alert_status="warning",
        alert_starts_at=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
    )
    result = make_result(status="critical", output_text="CRITICAL", finished_at=datetime(2026, 5, 21, 10, 5, tzinfo=timezone.utc))

    alerts = executor.update_alert_state(check, state, result)

    assert len(alerts) == 2
    assert alerts[0]["labels"]["status"] == "warning"
    assert alerts[0]["endsAt"] == result.finished_at.isoformat()
    assert alerts[1]["labels"]["status"] == "critical"
    assert "endsAt" not in alerts[1]


def test_execute_check_maps_timeout_to_unknown(monkeypatch):
    check = make_check(timeout=5.0)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=kwargs.get("args", check.command), timeout=5, output="", stderr="")

    monkeypatch.setattr(pluginexecutor.subprocess, "run", fake_run)

    result = pluginexecutor.execute_check(check)

    assert result.status == "unknown"
    assert result.exit_code is None
    assert "timed out" in result.stderr


def test_run_once_logs_and_sends_metrics(monkeypatch):
    check = make_check(output="always")
    config = pluginexecutor.AppConfig(checks=[check])
    state = pluginexecutor.CheckState()
    output_stream = io.StringIO()

    class MetricsStub:
        def __init__(self):
            self.calls = []

        def send_result(self, check_arg, state_arg, result_arg):
            self.calls.append((check_arg, state_arg.execution_count, result_arg.status))

    class AlertStub:
        def send_alerts(self, alerts):
            self.alerts = alerts

    metrics = MetricsStub()
    executor = pluginexecutor.PluginExecutor(config, metrics_client=metrics, alertmanager_client=AlertStub(), output_stream=output_stream)
    monkeypatch.setattr(pluginexecutor, "execute_check", lambda _check: make_result(status="ok", output_text="OK"))

    executor.run_once(check, state)

    assert state.execution_count == 1
    assert metrics.calls == [(check, 1, "ok")]
    assert "status=ok" in output_stream.getvalue()
