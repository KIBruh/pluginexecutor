from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class TLSOptions:
    verify: bool = True
    ca_file: Optional[str] = None
    cert_file: Optional[str] = None
    key_file: Optional[str] = None


@dataclass(frozen=True)
class EndpointConfig:
    enabled: bool = False
    url: Optional[str] = None
    tls_options: TLSOptions = field(default_factory=TLSOptions)

    def requests_kwargs(self) -> dict[str, Any]:
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
        default_factory=lambda: {"checkoutput": "{{ output_text }}"}
    )


@dataclass(frozen=True)
class AppConfig:
    checks: list[CheckConfig]
    metrics: EndpointConfig = field(default_factory=EndpointConfig)
    alertmanager: EndpointConfig = field(default_factory=EndpointConfig)
    max_workers: int = 10


@dataclass
class PerfDatum:
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
    execution_count: int = 0
    last_status: Optional[str] = None
    last_output: str = ""
    failing_since: Optional[datetime] = None
    alert_active: bool = False
    alert_status: Optional[str] = None
    alert_starts_at: Optional[datetime] = None


@dataclass(frozen=True)
class CheckResult:
    status: str
    exit_code: Optional[int]
    duration: float
    stdout: str
    stderr: str
    output_text: str
    perfdata: list[PerfDatum]
    finished_at: datetime
