from __future__ import annotations

import random
import subprocess
import time
from datetime import datetime, timezone
from typing import Optional

from ._constants import (
    MAX_SCHEDULING_JITTER_SECONDS,
    NUMERIC_RE,
    PERFDATA_RE,
    RANGE_NUMBER_RE,
    SCHEDULING_JITTER_RATIO,
)
from ._types import CheckConfig, CheckResult, PerfDatum


def map_exit_code(exit_code: Optional[int]) -> str:
    if exit_code is None:
        return "unknown"
    if exit_code == 0:
        return "ok"
    if exit_code == 1:
        return "warning"
    if exit_code == 2:
        return "critical"
    if exit_code == 3:
        return "unknown"
    return "out-of-bounds"


def compute_check_interval(check_period: float) -> float:
    jitter = min(MAX_SCHEDULING_JITTER_SECONDS, check_period * SCHEDULING_JITTER_RATIO)
    return max(0.0, check_period - random.uniform(0, jitter))


def normalize_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def execute_check(check: CheckConfig) -> CheckResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            check.command,
            capture_output=True,
            text=True,
            timeout=check.timeout,
            check=False,
            shell=False,
        )
        stdout = normalize_text(completed.stdout)
        stderr = normalize_text(completed.stderr)
        output_text, perfdata = parse_plugin_stdout(stdout)
        if not output_text:
            output_text = stderr.strip()
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = normalize_text(exc.stdout)
        stderr = normalize_text(exc.stderr)
        timeout_message = f"timed out after {check.timeout:g} seconds"
        stderr = f"{stderr.strip()} {timeout_message}".strip()
        output_text, perfdata = parse_plugin_stdout(stdout)
        if not output_text:
            output_text = timeout_message
        exit_code = None
    except OSError as exc:
        stdout = ""
        stderr = str(exc)
        output_text = stderr
        perfdata = []
        exit_code = None

    finished_at = datetime.now(timezone.utc)
    duration = time.monotonic() - started
    return CheckResult(
        status=map_exit_code(exit_code),
        exit_code=exit_code,
        duration=duration,
        stdout=stdout,
        stderr=stderr,
        output_text=output_text,
        perfdata=perfdata,
        finished_at=finished_at,
    )


def parse_plugin_stdout(stdout: str) -> tuple[str, list[PerfDatum]]:
    if not stdout:
        return "", []

    lines = stdout.strip().splitlines()
    if not lines:
        return "", []

    output_parts: list[str] = []
    perfdata_parts: list[str] = []
    for line in lines:
        text_part = line
        perfdata_part = ""
        if "|" in line:
            text_part, perfdata_part = line.split("|", 1)
        text_part = text_part.strip()
        if text_part:
            output_parts.append(text_part)
        perfdata_part = perfdata_part.strip()
        if perfdata_part:
            perfdata_parts.append(perfdata_part)

    parts = output_parts
    output_text = " ; ".join(part for part in parts if part)
    perfdata = parse_perfdata(" ".join(perfdata_parts))
    return output_text, perfdata


def parse_perfdata(perfdata_text: str) -> list[PerfDatum]:
    perfdata: list[PerfDatum] = []
    for token in split_perfdata_tokens(perfdata_text):
        match = PERFDATA_RE.match(token)
        if not match:
            continue
        value_text = normalize_perf_number(match.group("value"))
        if value_text is None:
            continue
        label = match.group("label")
        if label.startswith("'") and label.endswith("'"):
            label = label[1:-1]
        warn = parse_perf_threshold(match.group("warn"))
        crit = parse_perf_threshold(match.group("crit"))
        perfdata.append(
            PerfDatum(
                label=label,
                value=float(value_text),
                uom=match.group("uom") or "",
                warn=warn["value"],
                crit=crit["value"],
                warn_min=warn["minimum"],
                warn_max=warn["maximum"],
                warn_fill=warn["fill"],
                crit_min=crit["minimum"],
                crit_max=crit["maximum"],
                crit_fill=crit["fill"],
                minimum=parse_numeric_perf_field(match.group("minimum")),
                maximum=parse_numeric_perf_field(match.group("maximum")),
            )
        )
    return perfdata


def split_perfdata_tokens(perfdata_text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    in_quote = False
    for char in perfdata_text.strip():
        if char == "'":
            in_quote = not in_quote
            current.append(char)
            continue
        if char.isspace() and not in_quote:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def parse_numeric_perf_field(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    stripped = normalize_perf_number(value)
    if stripped is not None:
        return float(stripped)
    return None


def normalize_perf_number(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    stripped = value.strip().replace(",", ".")
    if stripped == "U":
        return None
    if NUMERIC_RE.match(stripped):
        return stripped
    return None


def parse_perf_threshold(value: Optional[str]) -> dict[str, Optional[float] | Optional[str]]:
    parsed: dict[str, Optional[float] | Optional[str]] = {
        "value": None,
        "minimum": None,
        "maximum": None,
        "fill": None,
    }
    if not value:
        return parsed

    stripped = value.strip().replace(",", ".")
    if not stripped:
        return parsed

    if NUMERIC_RE.match(stripped):
        parsed["value"] = float(stripped)
        parsed["fill"] = "none"
        return parsed

    fill = "outer"
    range_text = stripped
    if range_text.startswith("@"):
        fill = "inner"
        range_text = range_text[1:]

    if ":" not in range_text:
        return parsed

    lower_text, upper_text = range_text.split(":", 1)
    lower = parse_perf_range_bound(lower_text, allow_infinite_low=True)
    upper = parse_perf_range_bound(upper_text, allow_infinite_low=False)
    if lower is None and upper is None:
        return parsed

    parsed["minimum"] = lower
    parsed["maximum"] = upper
    parsed["fill"] = fill
    return parsed


def parse_perf_range_bound(value: str, *, allow_infinite_low: bool) -> Optional[float]:
    stripped = value.strip()
    if not stripped or (allow_infinite_low and stripped == "~"):
        return None
    match = RANGE_NUMBER_RE.search(stripped)
    if not match:
        return None
    return float(match.group(0))
