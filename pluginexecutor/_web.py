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
  *, *::before, *::after { box-sizing: border-box; }

  :root {
    --bg: #0f1318;
    --surface: #1a1f26;
    --surface-hover: #222830;
    --border: #2a3038;
    --text: #e1e4e8;
    --text-muted: #8b949e;
    --text-dim: #586069;
    --ok: #2ea043;
    --ok-bg: rgba(46, 160, 67, 0.12);
    --ok-border: rgba(46, 160, 67, 0.3);
    --warning: #d29922;
    --warning-bg: rgba(210, 153, 34, 0.12);
    --warning-border: rgba(210, 153, 34, 0.3);
    --critical: #da3633;
    --critical-bg: rgba(218, 54, 51, 0.12);
    --critical-border: rgba(218, 54, 51, 0.3);
    --unknown: #6e7681;
    --unknown-bg: rgba(110, 118, 129, 0.12);
    --unknown-border: rgba(110, 118, 129, 0.3);
    --oob: #da3633;
    --oob-bg: rgba(218, 54, 51, 0.12);
    --oob-border: rgba(218, 54, 51, 0.3);
    --input-bg: #0d1117;
    --radius: 8px;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
    --mono: "SF Mono", "Monaspace Neon", "Cascadia Code", "Fira Code", "JetBrains Mono", Consolas, monospace;
  }

  body {
    font-family: var(--font);
    margin: 0;
    padding: 24px;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    min-height: 100vh;
  }

  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 20px;
  }

  .header-left {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .header h1 {
    margin: 0;
    font-size: 1.375rem;
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  .header-badge {
    font-size: 0.6875rem;
    color: var(--text-dim);
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 2px 8px;
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 16px;
    font-size: 0.8125rem;
    color: var(--text-muted);
  }

  .last-refresh { font-variant-numeric: tabular-nums; }

  /* Status summary strip */
  .summary-strip {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }

  .summary-card {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: var(--surface);
    font-size: 0.8125rem;
  }

  .summary-card .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .summary-card .count {
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    min-width: 1.2em;
  }

  .sc-total { color: var(--text-muted); }
  .sc-ok .dot { background: var(--ok); }
  .sc-ok .count { color: var(--ok); }
  .sc-warning .dot { background: var(--warning); }
  .sc-warning .count { color: var(--warning); }
  .sc-critical .dot { background: var(--critical); }
  .sc-critical .count { color: var(--critical); }
  .sc-unknown .dot { background: var(--unknown); }
  .sc-unknown .count { color: var(--unknown); }

  /* Filters */
  .filters {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }

  .filter-group {
    position: relative;
    display: flex;
    align-items: center;
  }

  .filter-group .icon {
    position: absolute;
    left: 10px;
    color: var(--text-dim);
    font-size: 0.8125rem;
    pointer-events: none;
  }

  .filter-group input {
    background: var(--input-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-size: 0.8125rem;
    padding: 7px 10px 7px 28px;
    width: 200px;
    outline: none;
    transition: border-color 0.15s;
  }

  .filter-group input:focus {
    border-color: var(--text-muted);
  }

  .filter-group input::placeholder {
    color: var(--text-dim);
  }

  /* Table */
  .table-wrap {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    background: var(--surface);
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  thead {
    position: sticky;
    top: 0;
    z-index: 1;
  }

  th {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 10px 14px;
    text-align: left;
    font-weight: 600;
    color: var(--text-muted);
    white-space: nowrap;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  td {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }

  tbody tr:last-child td {
    border-bottom: none;
  }

  tbody tr:hover td {
    background: var(--surface-hover);
  }

  .host-cell {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .host-icon {
    width: 20px;
    height: 20px;
    border-radius: 4px;
    background: var(--border);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.6875rem;
    flex-shrink: 0;
    color: var(--text-dim);
  }

  /* Status badge */
  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    border: 1px solid transparent;
  }

  .status-badge .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }

  .status-ok { background: var(--ok-bg); border-color: var(--ok-border); color: var(--ok); }
  .status-ok .dot { background: var(--ok); }
  .status-warning { background: var(--warning-bg); border-color: var(--warning-border); color: var(--warning); }
  .status-warning .dot { background: var(--warning); }
  .status-critical { background: var(--critical-bg); border-color: var(--critical-border); color: var(--critical); }
  .status-critical .dot { background: var(--critical); }
  .status-unknown { background: var(--unknown-bg); border-color: var(--unknown-border); color: var(--unknown); }
  .status-unknown .dot { background: var(--unknown); }
  .status-oob { background: var(--oob-bg); border-color: var(--oob-border); color: var(--oob); }
  .status-oob .dot { background: var(--oob); }

  .output-cell {
    max-width: 380px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--text-muted);
    cursor: default;
  }

  .count-cell {
    text-align: right;
    font-variant-numeric: tabular-nums;
    color: var(--text-muted);
  }

  .next-cell {
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .next-cell .running {
    color: var(--warning);
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }

  .spinner {
    display: inline-block;
    width: 10px;
    height: 10px;
    border: 2px solid var(--warning-border);
    border-top-color: var(--warning);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .empty {
    text-align: center;
    padding: 40px 16px;
    color: var(--text-dim);
    font-size: 0.875rem;
  }

  .error-bar {
    margin-top: 8px;
    padding: 8px 14px;
    background: var(--critical-bg);
    border: 1px solid var(--critical-border);
    border-radius: var(--radius);
    color: var(--critical);
    font-size: 0.8125rem;
    display: none;
  }

  /* responsive */
  @media (max-width: 768px) {
    body { padding: 12px; }
    .header h1 { font-size: 1.125rem; }
    .filter-group input { width: 150px; }
    th, td { padding: 8px 10px; }
    .output-cell { max-width: 160px; }
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>Pluginexecutor</h1>
    <span class="header-badge" id="total-badge">0 checks</span>
  </div>
  <div class="header-right">
    <span class="last-refresh" id="last-refresh"></span>
  </div>
</div>

<div class="summary-strip" id="summary-strip"></div>

<div class="filters">
  <div class="filter-group">
    <span class="icon">&#128269;</span>
    <input type="text" id="filter-host" placeholder="Filter by host&hellip;">
  </div>
  <div class="filter-group">
    <span class="icon">&#128269;</span>
    <input type="text" id="filter-service" placeholder="Filter by service&hellip;">
  </div>
</div>

<div class="table-wrap">
<table>
  <thead>
    <tr>
      <th>Host</th>
      <th>Service</th>
      <th>Status</th>
      <th>Last Output</th>
      <th style="text-align:right">#</th>
      <th>Next Run</th>
    </tr>
  </thead>
  <tbody id="checks-body"></tbody>
</table>
</div>

<div class="error-bar" id="error-bar"></div>

<script>
  let checksCache = [];

  function escape(s) {
    var d = document.createElement("div");
    d.appendChild(document.createTextNode(s));
    return d.innerHTML;
  }

  function hostIcon(host) {
    return '<div class="host-icon">' + escape(host.charAt(0).toUpperCase()) + '</div>';
  }

  function statusBadge(status) {
    var cls = status ? "status-" + status : "status-unknown";
    var label = status ? status.toUpperCase() : "PENDING";
    return '<span class="status-badge ' + cls + '"><span class="dot"></span>' + label + '</span>';
  }

  function renderSummary() {
    var counts = {};
    checksCache.forEach(function(c) { counts[c.status || "unknown"] = (counts[c.status || "unknown"] || 0) + 1; });
    var total = checksCache.length;
    var html = '<div class="summary-card sc-total"><span class="count">' + total + '</span> Total</div>';
    var order = ["ok", "warning", "critical", "unknown"];
    order.forEach(function(s) {
      if (counts[s]) {
        html += '<div class="summary-card sc-' + s + '"><span class="dot"></span><span class="count">' + counts[s] + '</span> ' + s.charAt(0).toUpperCase() + s.slice(1) + '</div>';
      }
    });
    document.getElementById("summary-strip").innerHTML = html;
    document.getElementById("total-badge").textContent = total + (total === 1 ? " check" : " checks");
  }

  function render() {
    var hostFilter = document.getElementById("filter-host").value.toLowerCase();
    var svcFilter = document.getElementById("filter-service").value.toLowerCase();
    var filtered = checksCache.filter(function(c) {
      return (!hostFilter || c.host.toLowerCase().includes(hostFilter))
          && (!svcFilter || c.service.toLowerCase().includes(svcFilter));
    });
    var tbody = document.getElementById("checks-body");
    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="empty">' + (checksCache.length === 0 ? 'No checks loaded yet&hellip;' : 'No checks match the filter') + '</td></tr>';
      return;
    }
    tbody.innerHTML = filtered.map(function(c) {
      var nextRun;
      if (c.in_flight) {
        nextRun = '<span class="running"><span class="spinner"></span> running</span>';
      } else if (c.next_run_delta != null) {
        nextRun = c.next_run_delta + "s";
      } else {
        nextRun = "&mdash;";
      }
      return '<tr>' +
        '<td><div class="host-cell">' + hostIcon(c.host) + escape(c.host) + '</div></td>' +
        '<td>' + escape(c.service) + '</td>' +
        '<td>' + statusBadge(c.status) + '</td>' +
        '<td class="output-cell" title="' + escape(c.last_output || '') + '">' + escape(c.last_output || '') + '</td>' +
        '<td class="count-cell">' + c.execution_count + '</td>' +
        '<td class="next-cell">' + nextRun + '</td>' +
        '</tr>';
    }).join("");
    renderSummary();
  }

  var API_URL = "__MOUNTPOINT__/api/checks";

  async function fetchChecks() {
    try {
      var resp = await fetch(API_URL);
      var data = await resp.json();
      checksCache = data.checks;
      document.getElementById("last-refresh").textContent = "Updated " + new Date().toLocaleTimeString();
      document.getElementById("error-bar").style.display = "none";
      render();
    } catch(e) {
      document.getElementById("error-bar").textContent = "Fetch failed: " + e.message;
      document.getElementById("error-bar").style.display = "block";
    }
  }

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
                "last_output": state.last_output,
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
