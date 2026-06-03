from __future__ import annotations

import html
import json
import threading
import time
from typing import TYPE_CHECKING

from bottle import Bottle, HTTPResponse, request

from ._types import WebConfig

if TYPE_CHECKING:
    from ._executor import PluginExecutor


STATUS_CLASSES: dict[str | None, str] = {
    "ok": "status-ok",
    "warning": "status-warning",
    "critical": "status-critical",
    "unknown": "status-unknown",
    "out-of-bounds": "status-oob",
}
STATUS_LABELS: dict[str | None, str] = {
    "ok": "OK",
    "warning": "WARNING",
    "critical": "CRITICAL",
    "unknown": "UNKNOWN",
    "out-of-bounds": "OUT OF BOUNDS",
}

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PluginExecutor Status</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    margin: 0; padding: 20px; background: #f5f5f5; color: #333;
  }}
  h1 {{ margin: 0 0 16px 0; font-size: 1.5rem; }}
  .filters {{ margin-bottom: 16px; display: flex; gap: 12px; flex-wrap: wrap; }}
  .filters label {{ font-weight: 600; margin-right: 4px; }}
  .filters input {{
    padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; font-size: 0.875rem;
  }}
  .summary {{ margin-bottom: 12px; font-size: 0.875rem; color: #666; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 6px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #eee; font-size: 0.875rem; }}
  th {{ background: #fafafa; font-weight: 600; color: #555; white-space: nowrap; }}
  tr:hover td {{ background: #f8f9fa; }}
  .status-badge {{
    display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 0.75rem;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
  }}
  .status-ok {{ background: #d4edda; color: #155724; }}
  .status-warning {{ background: #fff3cd; color: #856404; }}
  .status-critical {{ background: #f8d7da; color: #721c24; }}
  .status-unknown {{ background: #e2e3e5; color: #383d41; }}
  .status-oob {{ background: #f8d7da; color: #721c24; }}
  .output-cell {{ max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: "SFMono-Regular", Consolas, monospace; font-size: 0.8125rem; }}
  .count-cell {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .next-cell {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  .empty {{ text-align: center; padding: 40px; color: #999; }}
  .refresh-note {{ margin-top: 8px; font-size: 0.75rem; color: #999; text-align: right; }}
</style>
</head>
<body>
<h1>Pluginexecutor Status</h1>
<div class="filters">
  <div><label for="filter-host">Host</label><input type="text" id="filter-host" placeholder="Filter by host&hellip;"></div>
  <div><label for="filter-service">Service</label><input type="text" id="filter-service" placeholder="Filter by service&hellip;"></div>
</div>
<div class="summary" id="summary"></div>
<table>
  <thead><tr><th>Host</th><th>Service</th><th>Status</th><th>Last Output</th><th>#</th><th>Next Run</th></tr></thead>
  <tbody id="checks-body"></tbody>
</table>
<div class="refresh-note" id="refresh-note"></div>
<script>
  let checksCache = [];

  function escape(s) {{
    const div = document.createElement("div");
    div.appendChild(document.createTextNode(s));
    return div.innerHTML;
  }}

  function statusBadge(status) {{
    const cls = status ? "status-" + status : "status-unknown";
    const label = status ? status.toUpperCase() : "PENDING";
    return '<span class="status-badge ' + cls + '">' + label + '</span>';
  }}

  function render() {{
    const hostFilter = document.getElementById("filter-host").value.toLowerCase();
    const svcFilter = document.getElementById("filter-service").value.toLowerCase();
    const filtered = checksCache.filter(function(c) {{
      return (!hostFilter || c.host.toLowerCase().includes(hostFilter))
          && (!svcFilter || c.service.toLowerCase().includes(svcFilter));
    }});
    document.getElementById("summary").textContent = filtered.length + " of " + checksCache.length + " checks";
    const tbody = document.getElementById("checks-body");
    if (filtered.length === 0) {{
      tbody.innerHTML = '<tr><td colspan="6" class="empty">No checks match the filter</td></tr>';
      return;
    }}
    tbody.innerHTML = filtered.map(function(c) {{
      var nextRun = c.in_flight ? "running&hellip;" : (c.next_run_delta != null ? c.next_run_delta + "s" : "&mdash;");
      return '<tr>' +
        '<td>' + escape(c.host) + '</td>' +
        '<td>' + escape(c.service) + '</td>' +
        '<td>' + statusBadge(c.status) + '</td>' +
        '<td class="output-cell" title="' + escape(c.last_output) + '">' + escape(c.last_output) + '</td>' +
        '<td class="count-cell">' + c.execution_count + '</td>' +
        '<td class="next-cell">' + nextRun + '</td>' +
        '</tr>';
    }}).join("");
  }}

  const API_URL = "__MOUNTPOINT__/api/checks";

  async function fetchChecks() {{
    try {{
      const resp = await fetch(API_URL);
      const data = await resp.json();
      checksCache = data.checks;
      render();
    }} catch(e) {{
      document.getElementById("refresh-note").textContent = "Fetch failed: " + e.message;
    }}
  }}

  document.getElementById("filter-host").addEventListener("input", render);
  document.getElementById("filter-service").addEventListener("input", render);
  setInterval(fetchChecks, 5000);
  fetchChecks();
</script>
</body>
</html>"""


class StatusWebServer:
    def __init__(self, executor: PluginExecutor, config: WebConfig) -> None:
        self.executor = executor
        self.config = config
        self.app = Bottle()
        self._setup_routes()
        self._thread: threading.Thread | None = None

    def _setup_routes(self) -> None:
        mp = self.config.mountpoint
        self.app.route(mp + "/", method="GET", callback=self._handle_index)
        self.app.route(mp + "/api/checks", method="GET", callback=self._handle_api_checks)

    def _snapshot(self) -> list[dict]:
        configs = self.executor.config.checks
        with self.executor._lock:
            states = list(self.executor.states)
            in_flight = list(self.executor._in_flight)
            next_run_times = list(self.executor._next_run_times)

        now = time.monotonic()
        checks: list[dict] = []
        for i, (cfg, state) in enumerate(zip(configs, states)):
            checks.append({
                "host": cfg.host,
                "service": cfg.service,
                "status": state.last_status,
                "last_output": state.last_output[-500:] if state.last_output else "",
                "execution_count": state.execution_count,
                "in_flight": in_flight[i],
                "next_run_delta": round(max(0.0, next_run_times[i] - now), 2),
            })
        return checks

    def _handle_api_checks(self) -> dict:
        checks = self._snapshot()
        return {
            "checks": checks,
            "total": len(checks),
            "max_workers": self.executor.config.max_workers,
        }

    def _handle_index(self) -> str:
        return DASHBOARD_HTML.replace("__MOUNTPOINT__", self.config.mountpoint)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=lambda: self.app.run(
                host=self.config.listen,
                port=self.config.port,
                quiet=True,
            ),
            daemon=True,
        )
        self._thread.start()
