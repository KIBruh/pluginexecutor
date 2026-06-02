from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import pluginexecutor
from pluginexecutor import _alerting, _executor, _logging, _plugin


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
        "template_context": {"host": "host-a", "service": "service-a"},
        "alert_annotations": {"checkoutput": "{{ output_text }}"},
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


def test_load_config_expands_grouped_targets_and_renders_command(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- targets:
  - host: host-1
    cluster: prod
  - host: host-2
    cluster: prod
  service: replication
  command:
  - /bin/check
  - --host
  - "{{ host }}"
  - --cluster
  - "{{ cluster }}"
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = pluginexecutor.load_config(config_path)

    assert len(config.checks) == 2
    assert config.checks[0].host == "host-1"
    assert config.checks[0].command == ["/bin/check", "--host", "host-1", "--cluster", "prod"]
    assert config.checks[1].host == "host-2"
    assert config.checks[1].template_context["cluster"] == "prod"


def test_load_config_renders_service_template(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- targets:
  - host: host-1
    cluster: prod
  - host: host-2
    cluster: staging
  service: "{{ host }} - {{ cluster }}"
  command:
  - /bin/check
  - --service
  - "{{ service }}"
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = pluginexecutor.load_config(config_path)

    assert config.checks[0].service == "host-1 - prod"
    assert config.checks[0].command == ["/bin/check", "--service", "host-1 - prod"]
    assert config.checks[1].service == "host-2 - staging"
    assert config.checks[1].command == ["/bin/check", "--service", "host-2 - staging"]


def test_load_config_templates_can_access_environment(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- host: localhost
  service: "{{ env.PLUGINEXECUTOR_SERVICE }}"
  command:
  - /bin/check
  - "{{ env.PLUGINEXECUTOR_COMMAND_ARG }}"
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PLUGINEXECUTOR_SERVICE", "env-service")
    monkeypatch.setenv("PLUGINEXECUTOR_COMMAND_ARG", "env-arg")

    config = pluginexecutor.load_config(config_path)

    assert config.checks[0].service == "env-service"
    assert config.checks[0].command == ["/bin/check", "env-arg"]


def test_load_config_templates_can_read_files(tmp_path):
    secret_path = tmp_path / "secret.txt"
    raw_path = tmp_path / "raw.txt"
    secret_path.write_text("token-value\n", encoding="utf-8")
    raw_path.write_text("line one\nline two\n", encoding="utf-8")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        f"""
checks:
- host: localhost
  service: "{{{{ '{secret_path}' | file }}}}"
  command:
  - /bin/check
  - "{{{{ '{raw_path}' | file(strip=False) }}}}"
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = pluginexecutor.load_config(config_path)

    assert config.checks[0].service == "token-value"
    assert config.checks[0].command == ["/bin/check", "line one\nline two\n"]


def test_load_config_command_templates_can_drop_arguments(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- host: localhost
  service: ping
  command:
  - /bin/check
  - "{{ drop_arg }}"
  - --host
  - "{{ host }}"
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = pluginexecutor.load_config(config_path)

    assert config.checks[0].command == ["/bin/check", "--host", "localhost"]


def test_load_config_rejects_command_when_all_arguments_are_dropped(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- host: localhost
  service: ping
  command:
  - "{{ drop_arg }}"
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must contain at least one argument"):
        pluginexecutor.load_config(config_path)


def test_load_config_rejects_target_key_mismatch(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- targets:
  - host: host-1
    cluster: prod
  - host: host-2
  service: replication
  command:
  - /bin/check
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly these keys"):
        pluginexecutor.load_config(config_path)


def test_load_config_rejects_target_without_host(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- targets:
  - cluster: prod
  service: replication
  command:
  - /bin/check
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="host"):
        pluginexecutor.load_config(config_path)


def test_load_config_rejects_missing_template_variable(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
checks:
- host: localhost
  service: ping
  command:
  - /bin/check
  - "{{ missing }}"
  check_period: 15
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="failed to render"):
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
    assert _plugin.map_exit_code(exit_code) == expected


def test_compute_check_interval_applies_bounded_jitter(monkeypatch):
    calls = []

    def fake_uniform(low, high):
        calls.append((low, high))
        return 0.6

    monkeypatch.setattr(_plugin.random, "uniform", fake_uniform)

    assert _plugin.compute_check_interval(60.0) == 59.4
    assert calls == [(0, 0.6)]


def test_compute_check_interval_caps_jitter_at_five_seconds(monkeypatch):
    calls = []

    def fake_uniform(low, high):
        calls.append((low, high))
        return 5.0

    monkeypatch.setattr(_plugin.random, "uniform", fake_uniform)

    assert _plugin.compute_check_interval(1000.0) == 995.0
    assert calls == [(0, 5.0)]


def test_compute_check_interval_floors_at_zero(monkeypatch):
    monkeypatch.setattr(_plugin.random, "uniform", lambda low, high: 5.0)

    assert _plugin.compute_check_interval(1.0) == 0.0


def test_parse_perfdata_supports_quoted_labels():
    perfdata = _plugin.parse_perfdata(
        "'queue depth'=4ms;10;20;;30 size=1.5GB;2;4;0;8 broken=5%;~:10;20"
    )

    assert perfdata[0] == pluginexecutor.PerfDatum(
        label="queue depth",
        value=4.0,
        uom="ms",
        warn=10.0,
        crit=20.0,
        warn_fill="none",
        crit_fill="none",
        maximum=30.0,
    )
    assert perfdata[1] == pluginexecutor.PerfDatum(
        label="size",
        value=1.5,
        uom="GB",
        warn=2.0,
        crit=4.0,
        warn_fill="none",
        crit_fill="none",
        minimum=0.0,
        maximum=8.0,
    )
    assert perfdata[2].warn is None
    assert perfdata[2].warn_max == 10.0
    assert perfdata[2].warn_fill == "outer"
    assert perfdata[2].crit == 20.0
    assert perfdata[2].crit_fill == "none"


def test_parse_plugin_stdout_collects_perfdata_from_all_pipe_segments():
    output_text, perfdata = _plugin.parse_plugin_stdout(
        "DISK OK - free space: / 3326 MB (56%); | /=2643MB;5948;5958;0;5968\n"
        "/boot 68 MB (69%); | /boot=68MB;88;93;0;98\n"
        "/home 69357 MB (27%);\n"
        "/var/log 819 MB (84%); | /var/log=818MB;970;975;0;980\n"
    )

    assert output_text == (
        "DISK OK - free space: / 3326 MB (56%); ; /boot 68 MB (69%); ; /home 69357 MB (27%); ; "
        "/var/log 819 MB (84%);"
    )
    assert [datum.label for datum in perfdata] == ["/", "/boot", "/var/log"]
    assert perfdata[0].minimum == 0.0
    assert perfdata[0].maximum == 5968.0


def test_parse_plugin_stdout_handles_perfdata_only_on_later_lines():
    output_text, perfdata = _plugin.parse_plugin_stdout(
        "PING OK\nRTA summary | rta=0.80ms;1;2;0;5\n"
    )

    assert output_text == "PING OK ; RTA summary"
    assert len(perfdata) == 1
    assert perfdata[0].label == "rta"


def test_parse_perfdata_supports_range_thresholds_for_numeric_metrics():
    perfdata = _plugin.parse_perfdata("latency=5ms;10:;@20:30;0;60")

    assert perfdata == [
        pluginexecutor.PerfDatum(
            label="latency",
            value=5.0,
            uom="ms",
            warn=None,
            crit=None,
            warn_min=10.0,
            warn_fill="outer",
            crit_min=20.0,
            crit_max=30.0,
            crit_fill="inner",
            minimum=0.0,
            maximum=60.0,
        )
    ]


def test_parse_perfdata_normalizes_comma_decimals_and_open_upper_ranges():
    perfdata = _plugin.parse_perfdata("temp=54,2C;40,5:60,5;70,0:;0;100")

    assert perfdata == [
        pluginexecutor.PerfDatum(
            label="temp",
            value=54.2,
            uom="C",
            warn_min=40.5,
            warn_max=60.5,
            warn_fill="outer",
            crit_min=70.0,
            crit_fill="outer",
            minimum=0.0,
            maximum=100.0,
        )
    ]


def test_should_log_output():
    assert _logging.should_log_output("always", "ok", "ok") is True
    assert _logging.should_log_output("state-change", "ok", "warning") is True
    assert _logging.should_log_output("state-change", "ok", "ok") is False
    assert _logging.should_log_output("non-ok", "ok", "critical") is True
    assert _logging.should_log_output("non-ok", "warning", "ok") is True
    assert _logging.should_log_output("never", None, "critical") is False


def test_build_victoriametrics_lines_include_status_and_perfdata():
    check = make_check()
    state = pluginexecutor.CheckState(execution_count=3)
    result = make_result(
        status="warning",
        perfdata=[
            pluginexecutor.PerfDatum(
                label="latency",
                value=1.5,
                uom="s",
                warn=3.0,
                crit=5.0,
                warn_fill="none",
                crit_fill="none",
                minimum=0.0,
                maximum=10.0,
            )
        ],
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
    assert "check_perf_min" in names
    assert "check_perf_max" in names

    perf_warn = [
        payload for payload in payloads if payload["metric"]["__name__"] == "check_perf_warn"
    ]
    assert perf_warn[0]["values"] == [3.0]
    assert perf_warn[0]["metric"]["threshold_fill"] == "none"

    warning_status = [
        payload
        for payload in payloads
        if payload["metric"]["__name__"] == "check_status"
        and payload["metric"]["status"] == "warning"
    ]
    assert warning_status[0]["values"] == [1]


def test_build_victoriametrics_lines_skip_non_numeric_warn_and_crit():
    check = make_check()
    state = pluginexecutor.CheckState(execution_count=1)
    result = make_result(
        perfdata=[
            pluginexecutor.PerfDatum(
                label="latency",
                value=1.5,
                uom="s",
                warn=None,
                crit=None,
                minimum=0.0,
                maximum=10.0,
            )
        ]
    )

    lines = pluginexecutor.VictoriaMetricsClient.build_lines(check, state, result)
    names = {json.loads(line)["metric"]["__name__"] for line in lines}

    assert "check_perf_value" in names
    assert "check_perf_min" in names
    assert "check_perf_max" in names
    assert "check_perf_warn" not in names
    assert "check_perf_crit" not in names


def test_build_victoriametrics_lines_include_threshold_ranges():
    check = make_check()
    state = pluginexecutor.CheckState(execution_count=1)
    result = make_result(
        perfdata=[
            pluginexecutor.PerfDatum(
                label="latency",
                value=1.5,
                uom="s",
                warn_min=10.0,
                warn_fill="outer",
                crit_min=20.0,
                crit_max=30.0,
                crit_fill="inner",
            )
        ]
    )

    lines = pluginexecutor.VictoriaMetricsClient.build_lines(check, state, result)
    payloads = [json.loads(line) for line in lines]

    warn_min = [
        payload for payload in payloads if payload["metric"]["__name__"] == "check_perf_warn_min"
    ]
    crit_min = [
        payload for payload in payloads if payload["metric"]["__name__"] == "check_perf_crit_min"
    ]
    crit_max = [
        payload for payload in payloads if payload["metric"]["__name__"] == "check_perf_crit_max"
    ]

    assert warn_min[0]["values"] == [10.0]
    assert warn_min[0]["metric"]["threshold_fill"] == "outer"
    assert crit_min[0]["values"] == [20.0]
    assert crit_min[0]["metric"]["threshold_fill"] == "inner"
    assert crit_max[0]["values"] == [30.0]
    assert crit_max[0]["metric"]["threshold_fill"] == "inner"


def test_build_alert_payload():
    check = make_check()
    starts_at = datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    ends_at = starts_at + timedelta(minutes=5)

    alert = pluginexecutor.AlertmanagerClient.build_alert(
        check, "critical", {"checkoutput": "disk full"}, starts_at, ends_at
    )

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
        alertmanager=pluginexecutor.EndpointConfig(
            enabled=True, url="https://alertmanager.example"
        ),
    )
    executor = pluginexecutor.PluginExecutor(config, output_stream=io.StringIO())
    check = config.checks[0]
    state = pluginexecutor.CheckState()
    first_result = make_result(
        status="critical", finished_at=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc)
    )
    second_result = make_result(
        status="critical",
        finished_at=datetime(2026, 5, 21, 10, 0, 31, tzinfo=timezone.utc),
    )

    assert executor.update_alert_state(check, state, first_result) == []
    alerts = executor.update_alert_state(check, state, second_result)

    assert len(alerts) == 1
    assert alerts[0]["labels"]["status"] == "critical"
    assert state.alert_active is True


def test_update_alert_state_resolves_and_refires_on_severity_change():
    config = pluginexecutor.AppConfig(
        checks=[make_check(notification_delay=0.0)],
        alertmanager=pluginexecutor.EndpointConfig(
            enabled=True, url="https://alertmanager.example"
        ),
    )
    executor = pluginexecutor.PluginExecutor(config, output_stream=io.StringIO())
    check = config.checks[0]
    state = pluginexecutor.CheckState(
        failing_since=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        alert_active=True,
        alert_status="warning",
        alert_starts_at=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
    )
    result = make_result(
        status="critical",
        output_text="CRITICAL",
        finished_at=datetime(2026, 5, 21, 10, 5, tzinfo=timezone.utc),
    )

    alerts = executor.update_alert_state(check, state, result)

    assert len(alerts) == 2
    assert alerts[0]["labels"]["status"] == "warning"
    assert alerts[0]["endsAt"] == result.finished_at.isoformat()
    assert alerts[1]["labels"]["status"] == "critical"
    assert "endsAt" not in alerts[1]


def test_render_alert_annotations_uses_default_checkoutput_template():
    check = make_check()
    result = make_result(status="critical", output_text="disk full")

    annotations = _alerting.render_alert_annotations(
        check, result, previous_status="ok", alert_status="critical"
    )

    assert annotations == {"checkoutput": "disk full"}


def test_render_alert_annotations_supports_custom_templates():
    check = make_check(
        host="db-1",
        service="replication",
        template_context={"host": "db-1", "service": "replication", "cluster": "prod"},
        alert_annotations={
            "summary": "{{ service }} on {{ host }} is {{ status }}",
            "description": "cluster={{ cluster }} current={{ current_status }} prev={{ previous_status }} msg={{ output_text }}",
        },
    )
    result = make_result(status="ok", output_text="recovered")

    annotations = _alerting.render_alert_annotations(
        check, result, previous_status="critical", alert_status="critical"
    )

    assert annotations["summary"] == "replication on db-1 is critical"
    assert annotations["description"] == "cluster=prod current=ok prev=critical msg=recovered"


def test_execute_check_maps_timeout_to_unknown(monkeypatch):
    check = make_check(timeout=5.0)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=kwargs.get("args", check.command), timeout=5, output="", stderr=""
        )

    monkeypatch.setattr(_plugin.subprocess, "run", fake_run)

    result = _plugin.execute_check(check)

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
    executor = pluginexecutor.PluginExecutor(
        config,
        metrics_client=metrics,
        alertmanager_client=AlertStub(),
        output_stream=output_stream,
    )
    monkeypatch.setattr(
        _executor,
        "execute_check",
        lambda _check: make_result(status="ok", output_text="OK"),
    )

    executor.run_once(check, state)

    assert state.execution_count == 1
    assert metrics.calls == [(check, 1, "ok")]
    assert "status=ok" in output_stream.getvalue()


def test_run_schedules_next_execution_after_one_interval(monkeypatch):
    check = make_check(check_period=60.0)
    executor = pluginexecutor.PluginExecutor(
        pluginexecutor.AppConfig(checks=[check]),
        output_stream=io.StringIO(),
    )

    fake_now = [0.0]
    execution_times: list[float] = []
    queued_tasks: list[tuple[object, tuple[object, ...]]] = []

    class FakePool:
        def __init__(self, max_workers):
            self.max_workers = max_workers

        def submit(self, fn, *args):
            queued_tasks.append((fn, args))

        def shutdown(self, wait=True):
            return None

    class FakeEvent:
        def __init__(self):
            self._set = False

        def is_set(self):
            return self._set

        def set(self):
            self._set = True

        def wait(self, timeout):
            if queued_tasks:
                fn, args = queued_tasks.pop(0)
                fn(*args)
            fake_now[0] += timeout
            return self._set

    def fake_execute(_check):
        execution_times.append(fake_now[0])
        if len(execution_times) >= 2:
            executor.stop_event.set()
        return make_result(status="ok", output_text="OK")

    monkeypatch.setattr(_executor, "ThreadPoolExecutor", FakePool)
    monkeypatch.setattr(_executor.time, "monotonic", lambda: fake_now[0])
    monkeypatch.setattr(_executor, "compute_check_interval", lambda period: period)
    monkeypatch.setattr(_executor, "execute_check", fake_execute)

    executor.stop_event = FakeEvent()
    executor._next_run_times = [0.0]
    executor.run()

    assert execution_times == pytest.approx([0.0, 60.0])
