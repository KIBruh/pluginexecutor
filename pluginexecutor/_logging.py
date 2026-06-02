from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from ._types import CheckConfig, CheckResult


def should_log_output(policy: str, previous_status: Optional[str], current_status: str) -> bool:
    if policy == "always":
        return True
    if policy == "state-change":
        return previous_status != current_status
    if policy == "non-ok":
        return previous_status != current_status or current_status != "ok"
    if policy == "never":
        return False
    raise ValueError(f"unknown output policy: {policy}")


def build_log_line(check: CheckConfig, result: CheckResult) -> str:
    exit_code = "none" if result.exit_code is None else str(result.exit_code)
    return (
        f"timestamp={result.finished_at.isoformat()} "
        f"host={json.dumps(check.host)} "
        f"service={json.dumps(check.service)} "
        f"status={result.status} "
        f"exit_code={exit_code} "
        f"duration={result.duration:.3f} "
        f"stdout={json.dumps(result.stdout.strip())} "
        f"stderr={json.dumps(result.stderr.strip())}"
    )


def emit_internal_log(message: str, stream: Any = None) -> None:
    target = stream or sys.stdout
    timestamp = datetime.now(timezone.utc).isoformat()
    print(
        f"timestamp={timestamp} component=pluginexecutor message={json.dumps(message)}",
        file=target,
        flush=True,
    )
