#!/usr/bin/env python3
"""
AWS Cost Viewer - Generates an HTML report of AWS costs using Cost Explorer API.
Usage: python3 cost-viewer.py [--months N]
Output: tools/report.html
"""

import subprocess
import sys
import json
import os
from datetime import datetime, timedelta
from calendar import monthrange

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
    import jinja2

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JPY_RATE = 150          # 1 USD = 150 JPY
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "report.html")

# ---------------------------------------------------------------------------
# AWS Cost Explorer helpers
# ---------------------------------------------------------------------------

def get_date_range():
    """Return (start, end) strings for the current month up to today."""
    today = datetime.today()
    start = today.replace(day=1).strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    # Cost Explorer requires end > start
    if start == end:
        end = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return start, end


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

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
      padding: 2rem;
    }

    h1 {
      font-size: 1.8rem;
      font-weight: 700;
      color: #f8fafc;
      margin-bottom: 0.25rem;
    }

    .subtitle {
      color: #94a3b8;
      font-size: 0.9rem;
      margin-bottom: 2rem;
    }

    /* Summary cards */
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 1rem;
      margin-bottom: 2.5rem;
    }

    .card {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 1.25rem 1.5rem;
    }

    .card-label {
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #64748b;
      margin-bottom: 0.4rem;
    }

    .card-value {
      font-size: 1.9rem;
      font-weight: 700;
      color: #38bdf8;
    }

    .card-value.jpy {
      color: #34d399;
    }

    .card-value.services {
      color: #a78bfa;
    }

    .card-sub {
      font-size: 0.8rem;
      color: #64748b;
      margin-top: 0.25rem;
    }

    /* Chart containers */
    .charts {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 1.5rem;
      margin-bottom: 2.5rem;
    }

    @media (max-width: 900px) {
      .charts { grid-template-columns: 1fr; }
    }

    .chart-box {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 1.5rem;
    }

    .chart-title {
      font-size: 1rem;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 1rem;
    }

    .chart-wrap {
      position: relative;
      height: 320px;
    }

    /* Table */
    .table-box {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 1.5rem;
      overflow-x: auto;
    }

    .table-title {
      font-size: 1rem;
      font-weight: 600;
      color: #cbd5e1;
      margin-bottom: 1rem;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.875rem;
    }

    thead th {
      text-align: left;
      padding: 0.6rem 1rem;
      background: #0f172a;
      color: #64748b;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    tbody tr:nth-child(even) { background: #162032; }
    tbody tr:hover { background: #1e3a5f; }

    tbody td {
      padding: 0.65rem 1rem;
      border-bottom: 1px solid #1e293b;
      color: #e2e8f0;
    }

    .amount-cell { text-align: right; font-variant-numeric: tabular-nums; }
    .bar-cell { min-width: 140px; }

    .bar-bg {
      background: #334155;
      border-radius: 4px;
      height: 8px;
      overflow: hidden;
    }

    .bar-fill {
      background: linear-gradient(90deg, #38bdf8, #818cf8);
      height: 100%;
      border-radius: 4px;
      transition: width 0.4s;
    }

    footer {
      margin-top: 2rem;
      text-align: center;
      color: #475569;
      font-size: 0.78rem;
    }
  </style>
</head>
<body>

  <h1>AWS Cost Report</h1>
  <p class="subtitle">期間: {{ period_start }} 〜 {{ period_end }} &nbsp;|&nbsp; 生成日時: {{ generated_at }}</p>

  <!-- Summary cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">合計コスト (USD)</div>
      <div class="card-value">${{ "%.2f"|format(total_usd) }}</div>
      <div class="card-sub">今月の累計</div>
    </div>
    <div class="card">
      <div class="card-label">合計コスト (円換算)</div>
      <div class="card-value jpy">¥{{ "{:,.0f}".format(total_jpy) }}</div>
      <div class="card-sub">1 USD = {{ jpy_rate }} 円</div>
    </div>
    <div class="card">
      <div class="card-label">利用サービス数</div>
      <div class="card-value services">{{ service_count }}</div>
      <div class="card-sub">コスト発生サービス</div>
    </div>
    <div class="card">
      <div class="card-label">最高コスト日</div>
      <div class="card-value" style="font-size:1.4rem">{{ peak_day }}</div>
      <div class="card-sub">${{ "%.2f"|format(peak_day_cost) }}</div>
    </div>
  </div>

  <!-- Charts -->
  <div class="charts">
    <div class="chart-box">
      <div class="chart-title">サービス別コスト (USD)</div>
      <div class="chart-wrap">
        <canvas id="barChart"></canvas>
      </div>
    </div>
    <div class="chart-box">
      <div class="chart-title">日別コスト推移 (USD)</div>
      <div class="chart-wrap">
        <canvas id="lineChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Service table -->
  <div class="table-box">
    <div class="table-title">サービス別コスト詳細</div>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>サービス</th>
          <th class="amount-cell">USD</th>
          <th class="amount-cell">円換算</th>
          <th class="amount-cell">割合</th>
          <th>比率</th>
        </tr>
      </thead>
      <tbody>
        {% for row in service_rows %}
        <tr>
          <td>{{ loop.index }}</td>
          <td>{{ row.service }}</td>
          <td class="amount-cell">${{ "%.4f"|format(row.amount) }}</td>
          <td class="amount-cell">¥{{ "{:,.0f}".format(row.amount * jpy_rate) }}</td>
          <td class="amount-cell">{{ "%.1f"|format(row.pct) }}%</td>
          <td class="bar-cell">
            <div class="bar-bg">
              <div class="bar-fill" style="width:{{ row.pct }}%"></div>
            </div>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <footer>Generated by cost-viewer.py &nbsp;&middot;&nbsp; AWS Cost Explorer API</footer>

  <script>
    // ---------- Bar chart: service costs ----------
    const barLabels = {{ bar_labels | tojson }};
    const barData   = {{ bar_data   | tojson }};

    new Chart(document.getElementById('barChart'), {
      type: 'bar',
      data: {
        labels: barLabels,
        datasets: [{
          label: 'Cost (USD)',
          data: barData,
          backgroundColor: 'rgba(56, 189, 248, 0.75)',
          borderColor: '#38bdf8',
          borderWidth: 1,
          borderRadius: 4,
        }]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => ` $${ctx.parsed.x.toFixed(4)}`
            }
          }
        },
        scales: {
          x: {
            ticks: { color: '#94a3b8', callback: v => '$' + v },
            grid: { color: '#1e293b' }
          },
          y: {
            ticks: {
              color: '#94a3b8',
              font: { size: 11 },
              callback: function(val) {
                const lbl = barLabels[val];
                return lbl && lbl.length > 28 ? lbl.substring(0, 26) + '…' : lbl;
              }
            },
            grid: { color: '#334155' }
          }
        }
      }
    });

    // ---------- Line chart: daily costs ----------
    const lineLabels = {{ line_labels | tojson }};
    const lineData   = {{ line_data   | tojson }};

    new Chart(document.getElementById('lineChart'), {
      type: 'line',
      data: {
        labels: lineLabels,
        datasets: [{
          label: 'Daily Cost (USD)',
          data: lineData,
          borderColor: '#818cf8',
          backgroundColor: 'rgba(129, 140, 248, 0.15)',
          borderWidth: 2,
          pointBackgroundColor: '#818cf8',
          pointRadius: 3,
          fill: true,
          tension: 0.3,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: ctx => ` $${ctx.parsed.y.toFixed(4)}`
            }
          }
        },
        scales: {
          x: {
            ticks: {
              color: '#94a3b8',
              maxTicksLimit: 10,
              callback: function(val) {
                const lbl = lineLabels[val];
                return lbl ? lbl.substring(5) : '';   // MM-DD
              }
            },
            grid: { color: '#1e293b' }
          },
          y: {
            ticks: { color: '#94a3b8', callback: v => '$' + v },
            grid: { color: '#334155' }
          }
        }
      }
    });
  </script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("AWS Cost Viewer")
    print("=" * 40)

    # Connect to Cost Explorer (us-east-1 is the only supported region)
    client = boto3.client("ce", region_name="us-east-1")

    start, end = get_date_range()
    print(f"期間: {start} 〜 {end}")

    print("サービス別コストを取得中...")
    service_data = fetch_cost_by_service(client, start, end)

    print("日別コストを取得中...")
    daily_data = fetch_daily_cost(client, start, end)

    print("合計コストを取得中...")
    total_usd = fetch_total_cost(client, start, end)

    # Derived values
    total_jpy = total_usd * JPY_RATE
    service_count = len(service_data)

    peak = max(daily_data, key=lambda x: x["amount"]) if daily_data else {"date": "-", "amount": 0}
    peak_day = peak["date"]
    peak_day_cost = peak["amount"]

    # Table rows with percentage
    for row in service_data:
        row["pct"] = round(row["amount"] / total_usd * 100, 1) if total_usd else 0

    # Chart data — top 15 services for readability
    top_services = service_data[:15]
    bar_labels = [r["service"] for r in top_services]
    bar_data   = [r["amount"]  for r in top_services]

    line_labels = [r["date"]   for r in daily_data]
    line_data   = [r["amount"] for r in daily_data]

    # Render HTML
    from jinja2 import Environment
    env = Environment()
    env.filters["tojson"] = json.dumps
    template = env.from_string(HTML_TEMPLATE)

    html = template.render(
        period_start=start,
        period_end=end,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_usd=total_usd,
        total_jpy=total_jpy,
        jpy_rate=JPY_RATE,
        service_count=service_count,
        peak_day=peak_day,
        peak_day_cost=peak_day_cost,
        service_rows=service_data,
        bar_labels=bar_labels,
        bar_data=bar_data,
        line_labels=line_labels,
        line_data=line_data,
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print()
    print(f"合計コスト: ${total_usd:.2f} USD / ¥{total_jpy:,.0f}")
    print(f"利用サービス数: {service_count}")
    print(f"レポート出力: {OUTPUT_PATH}")
    print("完了!")


if __name__ == "__main__":
    main()
