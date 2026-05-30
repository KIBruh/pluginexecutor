from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateError


def read_template_file(path: str, strip: bool = True) -> str:
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateError(str(exc)) from exc
    return content.strip() if strip else content


TEMPLATE_ENVIRONMENT = Environment(autoescape=False, undefined=StrictUndefined)
TEMPLATE_ENVIRONMENT.filters["file"] = read_template_file


def render_template(template: str, context: dict[str, Any], field_name: str) -> str:
    try:
        return TEMPLATE_ENVIRONMENT.from_string(template).render(context)
    except TemplateError as exc:
        raise ValueError(f"failed to render {field_name}: {exc}") from exc
