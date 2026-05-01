#!/usr/bin/env python3
"""
AWS Cost Viewer - Generates an HTML report of AWS costs using Cost Explorer API.
Usage: python3 cost-viewer.py [--month YYYY-MM]
Output: tools/report.html
"""

import argparse
import subprocess
import sys
import json
import os
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Dependency bootstrap
# ---------------------------------------------------------------------------

def pip_install(*packages):
    # Bootstrap pip via ensurepip if needed, then install
    subprocess.check_call(
        [sys.executable, "-m", "ensurepip", "--upgrade"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.check_call([sys.executable, "-m", "pip", "install", *packages, "-q"])

try:
    import boto3
except ImportError:
    print("Installing boto3...")
    pip_install("boto3")
    import boto3

try:
    import jinja2  # noqa: F401
except ImportError:
    print("Installing jinja2...")
    pip_install("jinja2")
    import jinja2  # noqa: F401

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JPY_RATE = 150          # 1 USD = 150 JPY
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "report.html")

# ---------------------------------------------------------------------------
# AWS Cost Explorer helpers
# ---------------------------------------------------------------------------

def _next_month_start(dt):
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1)
    return dt.replace(month=dt.month + 1, day=1)


def get_months_to_show(n):
    """Return list of (start, end, label) for the last n months with data."""
    today = datetime.today()
    cursor = today.replace(day=1)
    if cursor.date() == today.date():
        cursor = (cursor - timedelta(days=1)).replace(day=1)

    months = []
    for _ in range(n):
        start = cursor
        end = min(_next_month_start(cursor), today)
        label = f"{start.year}年{start.month}月"
        months.append((start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), label))
        cursor = (cursor - timedelta(days=1)).replace(day=1)

    return list(reversed(months))


def fetch_cost_by_service(client, start, end):
    """Return list of {service, amount} sorted descending."""
    resp = client.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    results = []
    for group in resp["ResultsByTime"][0]["Groups"]:
        service = group["Keys"][0]
        amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
        if amount > 0:
            results.append({"service": service, "amount": round(amount, 4)})
    results.sort(key=lambda x: x["amount"], reverse=True)
    return results


def fetch_daily_cost(client, start, end):
    """Return list of {date, amount} for every day in range."""
    resp = client.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
    )
    results = []
    for period in resp["ResultsByTime"]:
        date = period["TimePeriod"]["Start"]
        amount = float(period["Total"]["UnblendedCost"]["Amount"])
        results.append({"date": date, "amount": round(amount, 4)})
    return results


def fetch_total_cost(client, start, end):
    """Return total USD cost for the period."""
    resp = client.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
    )
    total = sum(
        float(p["Total"]["UnblendedCost"]["Amount"])
        for p in resp["ResultsByTime"]
    )
    return round(total, 4)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AWS Cost Report</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 2rem; }
    h1 { font-size: 1.8rem; font-weight: 700; color: #f8fafc; margin-bottom: 0.25rem; }
    .subtitle { color: #94a3b8; font-size: 0.9rem; margin-bottom: 1.5rem; }
    /* Tabs */
    .tabs { display: flex; gap: 0.5rem; margin-bottom: 2rem; flex-wrap: wrap; }
    .tab {
      padding: 0.5rem 1.25rem; border-radius: 8px; border: 1px solid #334155;
      background: #1e293b; color: #94a3b8; font-size: 0.95rem; cursor: pointer; transition: all 0.15s;
    }
    .tab:hover { background: #273549; color: #e2e8f0; }
    .tab.active { background: #38bdf8; border-color: #38bdf8; color: #0f172a; font-weight: 600; }
    /* Panels */
    .panel { display: none; }
    .panel.active { display: block; }
    /* Cards */
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2.5rem; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.25rem 1.5rem; }
    .card-label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; margin-bottom: 0.4rem; }
    .card-value { font-size: 1.9rem; font-weight: 700; color: #38bdf8; }
    .card-value.jpy { color: #34d399; }
    .card-value.services { color: #a78bfa; }
    .card-sub { font-size: 0.8rem; color: #64748b; margin-top: 0.25rem; }
    /* Charts */
    .charts { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }
    @media (max-width: 900px) { .charts { grid-template-columns: 1fr; } }
    .chart-box { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.5rem; }
    .chart-title { font-size: 1rem; font-weight: 600; color: #cbd5e1; margin-bottom: 1rem; }
    .chart-wrap { position: relative; height: 320px; }
    /* Table */
    .table-box { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.5rem; overflow-x: auto; }
    .table-title { font-size: 1rem; font-weight: 600; color: #cbd5e1; margin-bottom: 1rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
    thead th { text-align: left; padding: 0.6rem 1rem; background: #0f172a; color: #64748b; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }
    tbody tr:nth-child(even) { background: #162032; }
    tbody tr:hover { background: #1e3a5f; }
    tbody td { padding: 0.65rem 1rem; border-bottom: 1px solid #1e293b; color: #e2e8f0; }
    .amount-cell { text-align: right; font-variant-numeric: tabular-nums; }
    .bar-cell { min-width: 140px; }
    .bar-bg { background: #334155; border-radius: 4px; height: 8px; overflow: hidden; }
    .bar-fill { background: linear-gradient(90deg, #38bdf8, #818cf8); height: 100%; border-radius: 4px; transition: width 0.4s; }
    footer { margin-top: 2rem; text-align: center; color: #475569; font-size: 0.78rem; }
  </style>
</head>
<body>

  <h1>AWS Cost Report</h1>
  <p class="subtitle">生成日時: {{ generated_at }}</p>

  <!-- Tab buttons -->
  <div class="tabs">
    {% for m in months %}
    <button class="tab{% if loop.first %} active{% endif %}" onclick="showTab({{ loop.index0 }})">{{ m.label }}</button>
    {% endfor %}
  </div>

  <!-- Tab panels -->
  {% for m in months %}
  <div class="panel{% if loop.first %} active{% endif %}" id="panel-{{ loop.index0 }}">

    <div class="cards">
      <div class="card">
        <div class="card-label">合計コスト (USD)</div>
        <div class="card-value">${{ "%.2f"|format(m.total_usd) }}</div>
        <div class="card-sub">{{ m.start }} 〜 {{ m.end }}</div>
      </div>
      <div class="card">
        <div class="card-label">合計コスト (円換算)</div>
        <div class="card-value jpy">¥{{ "{:,.0f}".format(m.total_jpy) }}</div>
        <div class="card-sub">1 USD = {{ jpy_rate }} 円</div>
      </div>
      <div class="card">
        <div class="card-label">利用サービス数</div>
        <div class="card-value services">{{ m.service_count }}</div>
        <div class="card-sub">コスト発生サービス</div>
      </div>
      <div class="card">
        <div class="card-label">最高コスト日</div>
        <div class="card-value" style="font-size:1.4rem">{{ m.peak_day }}</div>
        <div class="card-sub">${{ "%.2f"|format(m.peak_day_cost) }}</div>
      </div>
    </div>

    <div class="charts">
      <div class="chart-box">
        <div class="chart-title">サービス別コスト (USD)</div>
        <div class="chart-wrap"><canvas id="bar-{{ loop.index0 }}"></canvas></div>
      </div>
      <div class="chart-box">
        <div class="chart-title">日別コスト推移 (USD)</div>
        <div class="chart-wrap"><canvas id="line-{{ loop.index0 }}"></canvas></div>
      </div>
    </div>

    <div class="table-box">
      <div class="table-title">サービス別コスト詳細</div>
      <table>
        <thead>
          <tr>
            <th>#</th><th>サービス</th>
            <th class="amount-cell">USD</th><th class="amount-cell">円換算</th>
            <th class="amount-cell">割合</th><th>比率</th>
          </tr>
        </thead>
        <tbody>
          {% for row in m.service_rows %}
          <tr>
            <td>{{ loop.index }}</td>
            <td>{{ row.service }}</td>
            <td class="amount-cell">${{ "%.4f"|format(row.amount) }}</td>
            <td class="amount-cell">¥{{ "{:,.0f}".format(row.amount * jpy_rate) }}</td>
            <td class="amount-cell">{{ "%.1f"|format(row.pct) }}%</td>
            <td class="bar-cell"><div class="bar-bg"><div class="bar-fill" style="width:{{ row.pct }}%"></div></div></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

  </div>
  {% endfor %}

  <footer>Generated by cost-viewer.py &nbsp;&middot;&nbsp; AWS Cost Explorer API</footer>

  <script>
    // All months' chart data
    const MONTHS = {{ months_js | tojson }};

    const charts = [];

    function makeBar(idx) {
      const bl = MONTHS[idx].bar_labels;
      const bd = MONTHS[idx].bar_data;
      return new Chart(document.getElementById('bar-' + idx), {
        type: 'bar',
        data: { labels: bl, datasets: [{ label: 'Cost (USD)', data: bd,
          backgroundColor: 'rgba(56,189,248,0.75)', borderColor: '#38bdf8', borderWidth: 1, borderRadius: 4 }] },
        options: {
          indexAxis: 'y', responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => ` $${ctx.parsed.x.toFixed(4)}` } } },
          scales: {
            x: { ticks: { color: '#94a3b8', callback: v => '$' + v }, grid: { color: '#1e293b' } },
            y: { ticks: { color: '#94a3b8', font: { size: 11 },
              callback: function(val) { const l = bl[val]; return l && l.length > 28 ? l.substring(0,26)+'…' : l; }
            }, grid: { color: '#334155' } }
          }
        }
      });
    }

    function makeLine(idx) {
      const ll = MONTHS[idx].line_labels;
      const ld = MONTHS[idx].line_data;
      return new Chart(document.getElementById('line-' + idx), {
        type: 'line',
        data: { labels: ll, datasets: [{ label: 'Daily Cost (USD)', data: ld,
          borderColor: '#818cf8', backgroundColor: 'rgba(129,140,248,0.15)',
          borderWidth: 2, pointBackgroundColor: '#818cf8', pointRadius: 3, fill: true, tension: 0.3 }] },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false }, tooltip: { callbacks: { label: ctx => ` $${ctx.parsed.y.toFixed(4)}` } } },
          scales: {
            x: { ticks: { color: '#94a3b8', maxTicksLimit: 10,
              callback: function(val) { const l = ll[val]; return l ? l.substring(5) : ''; }
            }, grid: { color: '#1e293b' } },
            y: { ticks: { color: '#94a3b8', callback: v => '$' + v }, grid: { color: '#334155' } }
          }
        }
      });
    }

    // Initialize charts for the first tab immediately; lazy-init others on first show
    const initialized = new Array(MONTHS.length).fill(false);

    function initTab(idx) {
      if (initialized[idx]) return;
      initialized[idx] = true;
      charts[idx] = { bar: makeBar(idx), line: makeLine(idx) };
    }

    function showTab(idx) {
      document.querySelectorAll('.tab').forEach((t, i) => t.classList.toggle('active', i === idx));
      document.querySelectorAll('.panel').forEach((p, i) => p.classList.toggle('active', i === idx));
      initTab(idx);
      charts[idx].bar.resize();
      charts[idx].line.resize();
    }

    // Init first tab on load
    initTab(0);
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _fetch_month(client, start, end, label):
    service_data = fetch_cost_by_service(client, start, end)
    daily_data   = fetch_daily_cost(client, start, end)
    total_usd    = fetch_total_cost(client, start, end)
    total_jpy    = total_usd * JPY_RATE
    for row in service_data:
        row["pct"] = round(row["amount"] / total_usd * 100, 1) if total_usd else 0
    peak = max(daily_data, key=lambda x: x["amount"]) if daily_data else {"date": "-", "amount": 0}
    top  = service_data[:15]
    return {
        "label":         label,
        "start":         start,
        "end":           end,
        "total_usd":     total_usd,
        "total_jpy":     total_jpy,
        "service_count": len(service_data),
        "peak_day":      peak["date"],
        "peak_day_cost": peak["amount"],
        "service_rows":  service_data,
        # chart data (also embedded as months_js for JS)
        "bar_labels":    [r["service"] for r in top],
        "bar_data":      [r["amount"]  for r in top],
        "line_labels":   [r["date"]    for r in daily_data],
        "line_data":     [r["amount"]  for r in daily_data],
    }


def main():
    parser = argparse.ArgumentParser(description="AWS Cost Viewer")
    parser.add_argument(
        "--months", type=int, default=2, metavar="N",
        help="取得する月数 (デフォルト: 2)",
    )
    args = parser.parse_args()

    print("AWS Cost Viewer")
    print("=" * 40)

    client = boto3.client("ce", region_name="us-east-1")

    months_data = []
    for start, end, label in get_months_to_show(args.months):
        print(f"{label} ({start} 〜 {end}) を取得中...")
        months_data.append(_fetch_month(client, start, end, label))

    # months_js: chart data only (passed to JS; keeps template clean)
    months_js = [
        {"bar_labels": m["bar_labels"], "bar_data": m["bar_data"],
         "line_labels": m["line_labels"], "line_data": m["line_data"]}
        for m in months_data
    ]

    from jinja2 import Environment
    env = Environment()
    env.filters["tojson"] = json.dumps
    template = env.from_string(HTML_TEMPLATE)

    html = template.render(
        months=months_data,
        months_js=months_js,
        jpy_rate=JPY_RATE,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print()
    for m in months_data:
        print(f"  {m['label']}: ${m['total_usd']:.2f} USD / ¥{m['total_jpy']:,.0f}")
    print(f"\nレポート出力: {OUTPUT_PATH}")
    print("完了!")


if __name__ == "__main__":
    main()
