#!/usr/bin/env python3
"""Generate a self-contained HTML dashboard from financials.db.

    python build_dashboard.py
    python build_dashboard.py --db financials.db --out financials_dashboard.html

One searchable/sortable table of every company (ticker, name, exchange,
source, latest-year headline numbers), KPI cards, a couple of summary charts,
and click-a-row to expand that company's key metrics as a small year x metric
grid (the nested-table view) -- without embedding all 1.2M+ raw line items,
which is far past what a browser-side dashboard should hold (see
KEY_METRICS below; same curated set export_financials.py uses).

Pure read-only queries against financials.db; safe to re-run any time to
refresh the dashboard after a new fetch.
"""
from __future__ import annotations

import argparse
import json
import sqlite3

KEY_METRICS = [
    "TotalRevenue", "NetIncome", "EBITDA", "BasicEPS",
    "TotalAssets", "TotalLiabilities", "StockholdersEquity",
    "CurrentAssets", "CurrentLiabilities", "CashAndCashEquivalents",
    "OperatingCashFlow", "FreeCashFlow",
]


def build_data(conn: sqlite3.Connection) -> dict:
    companies = conn.execute(
        "SELECT ticker, company_name, exchange, currency, primary_source "
        "FROM companies ORDER BY ticker"
    ).fetchall()

    placeholders = ", ".join("?" for _ in KEY_METRICS)
    rows = conn.execute(f"""
        SELECT sl.ticker, cy.fiscal_year, sl.line_item, sl.value
        FROM statement_lines sl
        JOIN company_years cy ON cy.ticker = sl.ticker AND cy.fiscal_year = sl.fiscal_year
        WHERE sl.line_item IN ({placeholders})
    """, KEY_METRICS).fetchall()

    # ticker -> year -> metric -> value  (the per-company nested grid)
    grid: dict[str, dict[int, dict[str, float | None]]] = {}
    for ticker, year, item, value in rows:
        grid.setdefault(ticker, {}).setdefault(year, {})[item] = value

    company_list = []
    for ticker, name, exchange, currency, source in companies:
        years = grid.get(ticker, {})
        latest_year = max(years) if years else None
        latest = years.get(latest_year, {}) if latest_year else {}
        company_list.append({
            "ticker": ticker,
            "name": name or "",
            "exchange": exchange or "",
            "source": source or "",
            "currency": currency or "",
            "latestYear": latest_year,
            "revenue": latest.get("TotalRevenue"),
            "netIncome": latest.get("NetIncome"),
            "totalAssets": latest.get("TotalAssets"),
        })

    # Not-yet-resolved companies too, so the dashboard reflects the WHOLE
    # attempted universe, not just the successes.
    status_counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM tmx_financials_status GROUP BY status"
    ).fetchall())

    # Simple list of everything attempted but NOT resolved (i.e. not in the
    # `companies` table, so not on the dashboard/table above) -- with why.
    missing = [
        {"ticker": t, "name": n or "", "exchange": ex or "", "status": s, "reason": r or ""}
        for t, n, ex, s, r in conn.execute(
            "SELECT ticker, company_name, exchange, status, reason "
            "FROM tmx_financials_status WHERE status != 'ok' ORDER BY exchange, ticker")
    ]

    exchange_counts = dict(conn.execute(
        "SELECT exchange, COUNT(*) FROM companies GROUP BY exchange ORDER BY 2 DESC"
    ).fetchall())

    source_counts = dict(conn.execute(
        "SELECT primary_source, COUNT(*) FROM companies GROUP BY primary_source"
    ).fetchall())

    # Resolution rate PER exchange -- attempted vs actually resolved ('ok'),
    # as a percentage. This replaces raw exchange-count KPI cards (which
    # don't say anything about how well each exchange resolved) with the
    # thing that's actually informative.
    attempted_by_exchange = dict(conn.execute(
        "SELECT exchange, COUNT(*) FROM tmx_financials_status GROUP BY exchange"
    ).fetchall())
    ok_by_exchange = dict(conn.execute(
        "SELECT exchange, COUNT(*) FROM tmx_financials_status WHERE status='ok' GROUP BY exchange"
    ).fetchall())
    resolution_by_exchange = [
        {"exchange": ex, "attempted": n, "resolved": ok_by_exchange.get(ex, 0),
         "pct": round(ok_by_exchange.get(ex, 0) / n * 100, 1) if n else 0}
        for ex, n in sorted(attempted_by_exchange.items())
    ]

    # Fiscal-year coverage: how many companies have a statement for each year.
    # QuoteMedia now returns up to ~14 years (was 5), so show the full window
    # (capped at 16 to bound the chart); older years naturally taper off but are
    # worth seeing now that the depth is there.
    year_coverage_rows = conn.execute(
        "SELECT fiscal_year, COUNT(DISTINCT ticker) FROM company_years "
        "GROUP BY fiscal_year ORDER BY fiscal_year DESC LIMIT 16"
    ).fetchall()
    year_coverage = sorted(({"year": y, "companies": n} for y, n in year_coverage_rows),
                           key=lambda r: r["year"])

    # Profitable vs unprofitable, latest fiscal year per company.
    profit_row = conn.execute("""
        SELECT
          SUM(CASE WHEN value > 0 THEN 1 ELSE 0 END),
          SUM(CASE WHEN value <= 0 THEN 1 ELSE 0 END)
        FROM statement_lines sl
        WHERE line_item = 'NetIncome' AND fiscal_year = (
          SELECT MAX(fiscal_year) FROM company_years cy WHERE cy.ticker = sl.ticker
        )
    """).fetchone()
    profitability = {"profitable": profit_row[0] or 0, "unprofitable": profit_row[1] or 0}

    avg_years_per_company = conn.execute(
        "SELECT AVG(cnt) FROM (SELECT ticker, COUNT(*) cnt FROM company_years GROUP BY ticker)"
    ).fetchone()[0] or 0

    total_lines = conn.execute("SELECT COUNT(*) FROM statement_lines").fetchone()[0]

    last_run = conn.execute(
        "SELECT run_at, total, resolved, failed, excluded, elapsed_sec "
        "FROM run_log ORDER BY run_at DESC LIMIT 1"
    ).fetchone()
    run_info = None
    if last_run:
        run_info = {"runAt": last_run[0], "total": last_run[1], "resolved": last_run[2],
                    "failed": last_run[3], "excluded": last_run[4], "elapsedSec": last_run[5]}

    top_revenue = sorted(
        (c for c in company_list if c["revenue"]),
        key=lambda c: c["revenue"], reverse=True
    )[:15]

    total_attempted = sum(status_counts.values())
    return {
        "companies": company_list,
        "grid": grid,  # ticker -> year -> metric -> value, for drill-down
        "statusCounts": status_counts,
        "exchangeCounts": exchange_counts,
        "sourceCounts": source_counts,
        "resolutionByExchange": resolution_by_exchange,
        "yearCoverage": year_coverage,
        "profitability": profitability,
        "avgYearsPerCompany": round(avg_years_per_company, 1),
        "totalLines": total_lines,
        "totalAttempted": total_attempted,
        "runInfo": run_info,
        "topRevenue": [{"ticker": c["ticker"], "name": c["name"], "revenue": c["revenue"]}
                       for c in top_revenue],
        "keyMetrics": KEY_METRICS,
        "missing": missing,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Canadian Companies — Financials Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1" integrity="sha384-jb8JQMbMoBUzgWatfe6COACi2ljcDdZQ2OxczGA3bGNeWe+6DChMTBJemed7ZnvJ" crossorigin="anonymous"></script>
<style>
:root {
    --bg-primary:#f8f9fa; --bg-card:#ffffff; --bg-header:#1a1a2e;
    --text-primary:#212529; --text-secondary:#6c757d; --text-on-dark:#ffffff;
    --color-1:#4C72B0; --color-2:#DD8452; --color-3:#55A868; --color-4:#C44E52;
    --positive:#28a745; --negative:#dc3545;
    --gap:16px; --radius:8px;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:var(--bg-primary); color:var(--text-primary); line-height:1.5; }
.dashboard-container { max-width:1400px; margin:0 auto; padding:var(--gap); }
.dashboard-header { background:var(--bg-header); color:var(--text-on-dark);
    padding:20px 24px; border-radius:var(--radius); margin-bottom:var(--gap);
    display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:12px; }
.dashboard-header h1 { font-size:20px; font-weight:600; }
.dashboard-header .subtitle { font-size:12px; color:rgba(255,255,255,.65); margin-top:2px; }
.filters { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
.filter-group { display:flex; align-items:center; gap:6px; }
.filter-group label { font-size:12px; color:rgba(255,255,255,.7); }
.filter-group select, .filter-group input[type=text] { padding:6px 10px; border:1px solid rgba(255,255,255,.2);
    border-radius:4px; background:rgba(255,255,255,.1); color:var(--text-on-dark); font-size:13px; }
.filter-group select option { background:var(--bg-header); color:var(--text-on-dark); }
.filter-group input::placeholder { color:rgba(255,255,255,.5); }
.kpi-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:var(--gap); margin-bottom:var(--gap); }
.kpi-card { background:var(--bg-card); border-radius:var(--radius); padding:20px 24px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
.kpi-label { font-size:12px; color:var(--text-secondary); text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
.kpi-value { font-size:28px; font-weight:700; }
.kpi-sub { font-size:12px; color:var(--text-secondary); margin-top:2px; }
.chart-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(400px,1fr)); gap:var(--gap); margin-bottom:var(--gap); }
.chart-container { background:var(--bg-card); border-radius:var(--radius); padding:20px 24px; box-shadow:0 1px 3px rgba(0,0,0,.08); }
.chart-container h3 { font-size:14px; font-weight:600; margin-bottom:16px; }
.chart-container canvas { max-height:280px; }
.table-section { background:var(--bg-card); border-radius:var(--radius); padding:20px 24px; box-shadow:0 1px 3px rgba(0,0,0,.08); overflow-x:auto; }
.table-section h3 { font-size:14px; font-weight:600; margin-bottom:12px; }
.missing-link { color:#ffd699; text-decoration:underline; cursor:pointer; }
.missing-link:hover { color:#fff; }
#missing-section { border-left:4px solid var(--color-2); background:#fffaf3; scroll-margin-top:16px; }
#missing-section h3 { color:#8a5a1f; }
#missing-section h3::before { content:"⚠ "; }
.data-table { width:100%; border-collapse:collapse; font-size:13px; }
.data-table thead th { text-align:left; padding:10px 12px; border-bottom:2px solid #dee2e6; color:var(--text-secondary);
    font-weight:600; font-size:11px; text-transform:uppercase; letter-spacing:.5px; white-space:nowrap; cursor:pointer; user-select:none; }
.data-table thead th:hover { color:var(--text-primary); background:#f8f9fa; }
.data-table tbody td { padding:9px 12px; border-bottom:1px solid #f0f0f0; }
.data-table tbody tr.company-row { cursor:pointer; }
.data-table tbody tr.company-row:hover { background:#f8f9fa; }
.num { text-align:right; font-variant-numeric:tabular-nums; }
.neg { color:var(--negative); }
.detail-row td { background:#fafbfc; padding:14px 20px; }
.detail-grid { border-collapse:collapse; font-size:12px; }
.detail-grid th, .detail-grid td { padding:5px 10px; border-bottom:1px solid #eee; text-align:right; }
.detail-grid th:first-child, .detail-grid td:first-child { text-align:left; color:var(--text-secondary); }
.detail-grid th { color:var(--text-secondary); font-weight:600; }
.pager { display:flex; gap:8px; align-items:center; margin-top:12px; font-size:13px; }
.pager button { padding:5px 10px; border:1px solid #dee2e6; background:#fff; border-radius:4px; cursor:pointer; }
.pager button:disabled { opacity:.4; cursor:default; }
.badge { display:inline-block; padding:2px 7px; border-radius:10px; font-size:11px; font-weight:600; }
.badge-tsx { background:#e7f0ff; color:var(--color-1); }
.badge-tsxv { background:#fff1e6; color:var(--color-2); }
.badge-xcnq { background:#e9f7ee; color:var(--color-3); }
.badge-neoe { background:#fdeaea; color:var(--color-4); }
.dashboard-footer { text-align:center; font-size:12px; color:var(--text-secondary); padding:16px 0; }
@media (max-width:768px) {
    .dashboard-header { flex-direction:column; align-items:flex-start; }
    .kpi-row { grid-template-columns:repeat(2,1fr); }
    .chart-row { grid-template-columns:1fr; }
    .filters { flex-direction:column; align-items:flex-start; }
}
</style>
</head>
<body>
<div class="dashboard-container">
    <header class="dashboard-header">
        <div>
            <h1>Canadian Companies — Financials Dashboard</h1>
            <div class="subtitle">Structured multi-year financials from QuoteMedia (TSX/TSXV/CSE/XCNQ/NEOE)
                &nbsp;&middot;&nbsp;<a href="#missing-section" class="missing-link" id="missing-nav-link">jump to missing companies</a></div>
        </div>
        <div class="filters">
            <div class="filter-group">
                <label for="f-search">Search</label>
                <input type="text" id="f-search" placeholder="Ticker or company name..." oninput="dash.applyFilters()">
            </div>
            <div class="filter-group">
                <label for="f-exchange">Exchange</label>
                <select id="f-exchange" onchange="dash.applyFilters()"><option value="all">All</option></select>
            </div>
        </div>
    </header>

    <section class="kpi-row">
        <div class="kpi-card"><div class="kpi-label">Companies Resolved</div>
            <div class="kpi-value" id="kpi-resolved"></div>
            <div class="kpi-sub" id="kpi-resolved-sub"></div></div>
        <div class="kpi-card"><div class="kpi-label">Data Points Collected</div>
            <div class="kpi-value" id="kpi-lines"></div>
            <div class="kpi-sub" id="kpi-lines-sub"></div></div>
        <div class="kpi-card"><div class="kpi-label">Avg. Years of History</div>
            <div class="kpi-value" id="kpi-avgyears"></div>
            <div class="kpi-sub">per company</div></div>
        <div class="kpi-card"><div class="kpi-label">Last Refreshed</div>
            <div class="kpi-value" id="kpi-lastrun"></div>
            <div class="kpi-sub" id="kpi-lastrun-sub"></div></div>
    </section>

    <section class="chart-row">
        <div class="chart-container"><h3>Companies by Exchange</h3><canvas id="chart-exchange"></canvas></div>
        <div class="chart-container"><h3>Resolution Rate by Exchange</h3><canvas id="chart-resolution"></canvas></div>
        <div class="chart-container"><h3>Top 15 by Revenue (latest year)</h3><canvas id="chart-revenue"></canvas></div>
        <div class="chart-container"><h3>Profitable vs. Unprofitable (latest year)</h3><canvas id="chart-profit"></canvas></div>
        <div class="chart-container"><h3>Companies Reporting, by Fiscal Year</h3><canvas id="chart-years"></canvas></div>
    </section>

    <section class="table-section">
        <h3>Companies <span id="table-count" style="color:var(--text-secondary);font-weight:400;"></span></h3>
        <table class="data-table">
            <thead><tr>
                <th data-field="ticker">Ticker</th>
                <th data-field="name">Company</th>
                <th data-field="exchange">Exchange</th>
                <th data-field="latestYear">Latest Yr</th>
                <th data-field="revenue" class="num">Revenue</th>
                <th data-field="netIncome" class="num">Net Income</th>
                <th data-field="totalAssets" class="num">Total Assets</th>
            </tr></thead>
            <tbody id="table-body"></tbody>
        </table>
        <div class="pager">
            <button id="pager-prev" onclick="dash.prevPage()">&larr; Prev</button>
            <span id="pager-label"></span>
            <button id="pager-next" onclick="dash.nextPage()">Next &rarr;</button>
        </div>
    </section>

    <section class="table-section" id="missing-section" style="margin-top:var(--gap);">
        <h3>Not on this dashboard <span style="color:var(--text-secondary);font-weight:400;" id="missing-count"></span></h3>
        <table class="data-table">
            <thead><tr>
                <th>Ticker</th><th>Company</th><th>Exchange</th><th>Status</th><th>Reason</th>
            </tr></thead>
            <tbody id="missing-body"></tbody>
        </table>
    </section>

    <footer class="dashboard-footer">Click any company row to expand its multi-year key-metric grid. Generated by build_dashboard.py from financials.db.</footer>
</div>

<script>
const DATA = __DATA_JSON__;
const PAGE_SIZE = 50;
const EXCHANGE_BADGE = {TSX:'badge-tsx', TSXV:'badge-tsxv', XCNQ:'badge-xcnq', NEOE:'badge-neoe'};

function fmtNum(v) {
    if (v === null || v === undefined) return '—';
    const sign = v < 0 ? '-' : '';
    const a = Math.abs(v);
    if (a >= 1e9) return sign + (a/1e9).toFixed(2) + 'B';
    if (a >= 1e6) return sign + (a/1e6).toFixed(1) + 'M';
    if (a >= 1e3) return sign + (a/1e3).toFixed(1) + 'K';
    return sign + a.toFixed(2);
}

class Dashboard {
    constructor(data) {
        this.data = data;
        this.filtered = data.companies;
        this.sortField = 'ticker';
        this.sortDir = 'asc';
        this.page = 0;
        this.expanded = new Set();
        this.init();
    }
    init() {
        this.populateExchangeFilter();
        this.renderKPIs();
        this.renderCharts();
        this.renderMissing();
        document.querySelectorAll('.data-table thead th').forEach(th => {
            th.addEventListener('click', () => this.sortBy(th.dataset.field));
        });
        this.applyFilters();
    }
    renderMissing() {
        const rows = this.data.missing;
        document.getElementById('missing-count').textContent = `(${rows.length})`;
        const tbody = document.getElementById('missing-body');
        tbody.innerHTML = '';
        const STATUS_LABEL = {
            no_data: 'No data', capital_pool_company: 'Capital Pool Co. (.P)',
            trust_or_fund: 'Trust / Fund (.UN)'
        };
        rows.forEach(r => {
            const tr = document.createElement('tr');
            const badge = EXCHANGE_BADGE[r.exchange] || '';
            tr.innerHTML = `<td><strong>${r.ticker}</strong></td><td>${r.name}</td>
                <td><span class="badge ${badge}">${r.exchange}</span></td>
                <td>${STATUS_LABEL[r.status] || r.status}</td>
                <td style="color:var(--text-secondary);">${r.reason}</td>`;
            tbody.appendChild(tr);
        });
    }
    populateExchangeFilter() {
        const sel = document.getElementById('f-exchange');
        Object.keys(this.data.exchangeCounts).sort().forEach(ex => {
            const o = document.createElement('option');
            o.value = ex; o.textContent = `${ex} (${this.data.exchangeCounts[ex]})`;
            sel.appendChild(o);
        });
    }
    renderKPIs() {
        const total = this.data.totalAttempted;
        const resolved = this.data.companies.length;
        document.getElementById('kpi-resolved').textContent = resolved.toLocaleString();
        document.getElementById('kpi-resolved-sub').textContent =
            `of ${total.toLocaleString()} attempted (${(resolved/total*100).toFixed(1)}%)`;

        document.getElementById('kpi-lines').textContent = this.data.totalLines.toLocaleString();
        document.getElementById('kpi-lines-sub').textContent = 'individual financial figures';

        document.getElementById('kpi-avgyears').textContent = this.data.avgYearsPerCompany.toFixed(1);

        const run = this.data.runInfo;
        if (run) {
            const d = new Date(run.runAt);
            document.getElementById('kpi-lastrun').textContent =
                d.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'});
            document.getElementById('kpi-lastrun-sub').textContent =
                `fetched ${run.total.toLocaleString()} companies in ${run.elapsedSec.toFixed(1)}s`;
        } else {
            document.getElementById('kpi-lastrun').textContent = '—';
            document.getElementById('kpi-lastrun-sub').textContent = 'no run_log entry';
        }
    }
    renderCharts() {
        const ec = this.data.exchangeCounts;
        new Chart(document.getElementById('chart-exchange').getContext('2d'), {
            type: 'doughnut',
            data: { labels: Object.keys(ec), datasets: [{ data: Object.values(ec),
                backgroundColor: ['#4C72B0CC','#DD8452CC','#55A868CC','#C44E52CC'], borderColor:'#fff', borderWidth:2 }] },
            options: { responsive:true, maintainAspectRatio:false, cutout:'60%',
                plugins: { legend: { position:'right', labels:{usePointStyle:true, padding:12} } } }
        });
        const top = this.data.topRevenue;
        new Chart(document.getElementById('chart-revenue').getContext('2d'), {
            type: 'bar',
            data: { labels: top.map(c => c.ticker), datasets: [{ data: top.map(c => c.revenue),
                backgroundColor: '#4C72B0CC', borderRadius:4 }] },
            options: { responsive:true, maintainAspectRatio:false, indexAxis:'y',
                plugins: { legend:{display:false},
                    tooltip: { callbacks: { label: c => fmtNum(c.parsed.x) } } },
                scales: { x: { ticks: { callback: v => fmtNum(v) } } } }
        });

        // Resolution rate by exchange -- the percentage-based replacement
        // for the old raw exchange-count KPI cards.
        const rbe = this.data.resolutionByExchange;
        new Chart(document.getElementById('chart-resolution').getContext('2d'), {
            type: 'bar',
            data: { labels: rbe.map(r => r.exchange), datasets: [{ data: rbe.map(r => r.pct),
                backgroundColor: '#55A868CC', borderRadius:4 }] },
            options: { responsive:true, maintainAspectRatio:false,
                plugins: { legend:{display:false},
                    tooltip: { callbacks: { label: c => {
                        const r = rbe[c.dataIndex];
                        return `${r.pct}% (${r.resolved.toLocaleString()} of ${r.attempted.toLocaleString()})`;
                    } } } },
                scales: { y: { beginAtZero:true, max:100, ticks: { callback: v => v + '%' } } } }
        });

        // Profitable vs unprofitable, latest fiscal year per company.
        const pr = this.data.profitability;
        new Chart(document.getElementById('chart-profit').getContext('2d'), {
            type: 'doughnut',
            data: { labels: ['Profitable', 'Unprofitable'],
                datasets: [{ data: [pr.profitable, pr.unprofitable],
                    backgroundColor: ['#55A868CC','#C44E52CC'], borderColor:'#fff', borderWidth:2 }] },
            options: { responsive:true, maintainAspectRatio:false, cutout:'60%',
                plugins: { legend: { position:'right', labels:{usePointStyle:true, padding:12} },
                    tooltip: { callbacks: { label: c => {
                        const total = pr.profitable + pr.unprofitable;
                        return `${c.label}: ${c.parsed.toLocaleString()} (${(c.parsed/total*100).toFixed(1)}%)`;
                    } } } } }
        });

        // Fiscal-year coverage -- how much depth of history is actually here.
        const yc = this.data.yearCoverage;
        new Chart(document.getElementById('chart-years').getContext('2d'), {
            type: 'bar',
            data: { labels: yc.map(y => y.year), datasets: [{ data: yc.map(y => y.companies),
                backgroundColor: '#DD8452CC', borderRadius:4 }] },
            options: { responsive:true, maintainAspectRatio:false,
                plugins: { legend:{display:false},
                    tooltip: { callbacks: { label: c => `${c.parsed.y.toLocaleString()} companies` } } },
                scales: { y: { beginAtZero:true, ticks: { callback: v => fmtNum(v) } } } }
        });
    }
    applyFilters() {
        const q = document.getElementById('f-search').value.trim().toLowerCase();
        const ex = document.getElementById('f-exchange').value;
        this.filtered = this.data.companies.filter(c => {
            if (ex !== 'all' && c.exchange !== ex) return false;
            if (q && !c.ticker.toLowerCase().includes(q) && !c.name.toLowerCase().includes(q)) return false;
            return true;
        });
        this.page = 0;
        this.sortAndRender();
    }
    sortBy(field) {
        if (this.sortField === field) { this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc'; }
        else { this.sortField = field; this.sortDir = 'asc'; }
        this.sortAndRender();
    }
    sortAndRender() {
        const f = this.sortField, d = this.sortDir === 'asc' ? 1 : -1;
        this.filtered.sort((a,b) => {
            let av = a[f], bv = b[f];
            if (av === null || av === undefined) av = -Infinity;
            if (bv === null || bv === undefined) bv = -Infinity;
            if (typeof av === 'string') return d * av.localeCompare(bv);
            return d * (av - bv);
        });
        this.renderTable();
    }
    renderTable() {
        document.getElementById('table-count').textContent = `(${this.filtered.length.toLocaleString()})`;
        const start = this.page * PAGE_SIZE;
        const pageRows = this.filtered.slice(start, start + PAGE_SIZE);
        const tbody = document.getElementById('table-body');
        tbody.innerHTML = '';
        pageRows.forEach(c => {
            const tr = document.createElement('tr');
            tr.className = 'company-row';
            const badge = EXCHANGE_BADGE[c.exchange] || '';
            tr.innerHTML = `<td><strong>${c.ticker}</strong></td><td>${c.name}</td>
                <td><span class="badge ${badge}">${c.exchange}</span></td>
                <td>${c.latestYear ?? '—'}</td>
                <td class="num">${fmtNum(c.revenue)}</td>
                <td class="num ${c.netIncome < 0 ? 'neg' : ''}">${fmtNum(c.netIncome)}</td>
                <td class="num">${fmtNum(c.totalAssets)}</td>`;
            tr.addEventListener('click', () => this.toggleDetail(c.ticker, tr));
            tbody.appendChild(tr);
        });
        const totalPages = Math.max(1, Math.ceil(this.filtered.length / PAGE_SIZE));
        document.getElementById('pager-label').textContent = `Page ${this.page+1} of ${totalPages}`;
        document.getElementById('pager-prev').disabled = this.page === 0;
        document.getElementById('pager-next').disabled = this.page >= totalPages - 1;
    }
    toggleDetail(ticker, tr) {
        const existing = tr.nextElementSibling;
        if (existing && existing.classList.contains('detail-row')) { existing.remove(); return; }
        document.querySelectorAll('.detail-row').forEach(r => r.remove());
        const years = this.data.grid[ticker] || {};
        const sortedYears = Object.keys(years).sort((a,b) => b - a);
        if (!sortedYears.length) return;
        let html = '<table class="detail-grid"><thead><tr><th>Metric</th>' +
            sortedYears.map(y => `<th>${y}</th>`).join('') + '</tr></thead><tbody>';
        this.data.keyMetrics.forEach(m => {
            const vals = sortedYears.map(y => fmtNum(years[y][m]));
            if (vals.every(v => v === '—')) return;
            html += `<tr><th>${m}</th>` + vals.map(v => `<td>${v}</td>`).join('') + '</tr>';
        });
        html += '</tbody></table>';
        const detailTr = document.createElement('tr');
        detailTr.className = 'detail-row';
        detailTr.innerHTML = `<td colspan="7">${html}</td>`;
        tr.insertAdjacentElement('afterend', detailTr);
    }
    prevPage() { if (this.page > 0) { this.page--; this.renderTable(); } }
    nextPage() {
        const totalPages = Math.ceil(this.filtered.length / PAGE_SIZE);
        if (this.page < totalPages - 1) { this.page++; this.renderTable(); }
    }
}
const dash = new Dashboard(DATA);
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="output/financials.db")
    ap.add_argument("--out", default="output/financials_dashboard.html")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        data = build_data(conn)
    finally:
        conn.close()

    html = HTML_TEMPLATE.replace("__DATA_JSON__", json.dumps(data))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    import os
    size_mb = os.path.getsize(args.out) / 1_000_000
    print(f"Wrote {args.out} ({size_mb:.1f} MB, {len(data['companies'])} companies)")


if __name__ == "__main__":
    main()
