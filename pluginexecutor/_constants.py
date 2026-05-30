from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, StrictUndefined, TemplateError


STATUS_NAMES = ("ok", "warning", "critical", "unknown", "out-of-bounds")
OUTPUT_POLICIES = frozenset({"always", "state-change", "non-ok", "never"})
DEFAULT_ALERT_ANNOTATIONS = {"checkoutput": "{{ output_text }}"}
TEMPLATE_ENVIRONMENT = Environment(autoescape=False, undefined=StrictUndefined)
INTERNAL_TEMPLATE_CONTEXT_KEY = "__template_context"
INTERNAL_ALERT_ANNOTATIONS_KEY = "__alert_annotation_templates"
COMMAND_ARG_DROP_SENTINEL = "__PLUGINEXECUTOR_DROP_ARG__"
MAX_SCHEDULING_JITTER_SECONDS = 5.0
SCHEDULING_JITTER_RATIO = 0.01
NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
RANGE_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
PERFDATA_RE = re.compile(
    r"^(?P<label>'[^']+'|[^=\s]+)="
    r"(?P<value>U|[+-]?(?:\d+(?:[\.,]\d*)?|[\.,]\d+))"
    r"(?P<uom>[^;\s]*)"
    r"(?:;(?P<warn>[^;]*))?"
    r"(?:;(?P<crit>[^;]*))?"
    r"(?:;(?P<minimum>[^;]*))?"
    r"(?:;(?P<maximum>[^;]*))?$"
)


def read_template_file(path: str, strip: bool = True) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(str(exc)) from exc
    return content.strip() if strip else content


TEMPLATE_ENVIRONMENT.filters["file"] = read_template_file
