from __future__ import annotations

import re


STATUS_NAMES = ("ok", "warning", "critical", "unknown", "out-of-bounds")
OUTPUT_POLICIES = frozenset({"always", "state-change", "non-ok", "never"})
DEFAULT_ALERT_ANNOTATIONS = {"checkoutput": "{{ output_text }}"}
INTERNAL_TEMPLATE_CONTEXT_KEY = "__template_context"
INTERNAL_ALERT_ANNOTATIONS_KEY = "__alert_annotation_templates"
COMMAND_ARG_DROP_SENTINEL = "__PLUGINEXECUTOR_DROP_ARG__"
MAX_SCHEDULING_JITTER_SECONDS = 5.0
SCHEDULING_JITTER_RATIO = 0.01
NUMERIC_PATTERN = r"""
    [+-]?              # optional sign
    (?:
        \d+           # integer part
        (?:\.\d*)?   # optional decimal point and fractional digits
      |
        \.\d+        # decimal value without leading integer part
    )
"""

NUMERIC_RE = re.compile(r"^" + NUMERIC_PATTERN + r"$", re.X)
RANGE_NUMBER_RE = re.compile(NUMERIC_PATTERN, re.X)
PERFDATA_RE = re.compile(
    r"""
    ^                                  # start of perfdata item
    (?P<label>
        '[^']+'                        # quoted label
      |
        [^=\s]+                       # unquoted label up to '=' or whitespace
    )
    =                                  # label/value separator
    (?P<value>
        U                              # unknown value marker
      |
        [+-]?                          # optional sign
        (?:
            \d+                       # integer part
            (?:[\.,]\d*)?            # optional decimal separator and fraction
          |
            [\.,]\d+                 # decimal value without leading integer part
        )
    )
    (?P<uom>[^;\s]*)                  # optional unit of measure
    (?:;(?P<warn>[^;]*))?              # optional warning threshold
    (?:;(?P<crit>[^;]*))?              # optional critical threshold
    (?:;(?P<minimum>[^;]*))?           # optional minimum value
    (?:;(?P<maximum>[^;]*))?           # optional maximum value
    $                                  # end of perfdata item
    """,
    re.X,
)
