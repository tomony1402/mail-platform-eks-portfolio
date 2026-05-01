#!/usr/bin/env python3
"""
Admin Server - Mail Platform EKS  (port 8080)
Features:
  - AWS cost visualization  (cost-viewer.py → report.html)
  - HELO ConfigMap editor + kubectl apply / rollout restart
  - Sender-canonical ConfigMap editor + kubectl apply / rollout restart
  - Queue snapshots (scheduled at 0, 3, 6 JST) + S3 recovery stats
  - Node snapshots (scheduled at 23, 3, 7 JST)
  - Cluster metrics (nodes / pods / CronJob status)
  - Basic auth: admin / <password shown at startup>
"""

import base64
import concurrent.futures
import json
import os
import re as _re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).parent.resolve()
REPO_ROOT      = SCRIPT_DIR.parent
COST_VIEWER    = SCRIPT_DIR / "cost-viewer.py"
REPORT_HTML    = SCRIPT_DIR / "report.html"
CONFIGMAP_PATH          = REPO_ROOT / "manifests" / "postfix" / "configmap-helo.yaml"
RECIPIENT_CANONICAL_PATH = REPO_ROOT / "manifests" / "postfix" / "configmap-recipient-canonical.yaml"
QUEUE_SNAPSHOTS_PATH    = SCRIPT_DIR / "queue-snapshots.json"
NODE_SNAPSHOTS_PATH     = SCRIPT_DIR / "node-snapshots.json"
RECOVERY_STATS_PATH     = SCRIPT_DIR / "recovery-stats.json"
S3_RECOVERY_BUCKET      = "<YOUR_S3_RECOVERY_BUCKET>"

# ── Auth ──────────────────────────────────────────────────────────────────────
USERNAME = "admin"
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# ── Cost viewer state (guarded by _cost_lock) ─────────────────────────────────
_cost_lock   = threading.Lock()
_cost_status = "idle"   # idle | running | done | error
_cost_log    = ""

# ── Queue monitor state ────────────────────────────────────────────────────────
JST = timezone(timedelta(hours=9))
_queue_lock      = threading.Lock()
# Scheduled snapshot hours in JST
_QUEUE_SNAP_HOURS = (0, 3, 6)
# { "HH:MM": {"timestamp": ISO, "pods": {pod_name: count}} }
_queue_snapshots: dict = {}
_queue_snap_last_taken: dict = {}   # {hour: date_str} so we take once per day


def _load_queue_snapshots():
    """Load persisted snapshots from disk on startup."""
    global _queue_snapshots, _queue_snap_last_taken
    try:
        if QUEUE_SNAPSHOTS_PATH.exists():
            data = json.loads(QUEUE_SNAPSHOTS_PATH.read_text())
            _queue_snapshots = data.get("snapshots", {})
            _queue_snap_last_taken = {int(k): v for k, v in data.get("last_taken", {}).items()}
    except Exception:
        pass


def _save_queue_snapshots():
    """Persist snapshots to disk (called under _queue_lock)."""
    try:
        data = {
            "snapshots": _queue_snapshots,
            "last_taken": _queue_snap_last_taken,
        }
        QUEUE_SNAPSHOTS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


_load_queue_snapshots()


# ── Node snapshot state ────────────────────────────────────────────────────────
_node_lock      = threading.Lock()
# Scheduled snapshot hours in JST
_NODE_SNAP_HOURS = (23, 3, 7)
# { "23:00": {"timestamp": ISO, "nodes": [{"name":..,"instance_type":..,"capacity_type":..}]} }
_node_snapshots: dict = {}
_node_snap_last_taken: dict = {}   # {hour: date_str} so we take once per day


def _load_node_snapshots():
    """Load persisted node snapshots from disk on startup."""
    global _node_snapshots, _node_snap_last_taken
    try:
        if NODE_SNAPSHOTS_PATH.exists():
            data = json.loads(NODE_SNAPSHOTS_PATH.read_text())
            _node_snapshots = data.get("snapshots", {})
            _node_snap_last_taken = {int(k): v for k, v in data.get("last_taken", {}).items()}
    except Exception:
        pass


def _save_node_snapshots():
    """Persist node snapshots to disk (called under _node_lock)."""
    try:
        data = {
            "snapshots": _node_snapshots,
            "last_taken": _node_snap_last_taken,
        }
        NODE_SNAPSHOTS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:
        pass


_load_node_snapshots()


def _collect_node_data() -> list:
    """kubectl get nodes を実行してノードリストを返す。"""
    r = subprocess.run(
        ["kubectl", "get", "nodes", "-o", "json"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"kubectl error (exit {r.returncode}): {r.stderr.strip() or '(no stderr)'}")
    nodes = []
    for item in json.loads(r.stdout).get("items", []):
        labels = item["metadata"].get("labels", {})
        instance_type = labels.get("node.kubernetes.io/instance-type", "")
        raw = (
            labels.get("eks.amazonaws.com/capacityType") or
            labels.get("karpenter.sh/capacity-type") or
            labels.get("node.kubernetes.io/lifecycle") or
            ""
        )
        ct = raw.upper().replace("-", "_")
        if ct == "SPOT":
            capacity_type = "SPOT"
        elif ct in ("ON_DEMAND", "NORMAL"):
            capacity_type = "ON_DEMAND"
        else:
            capacity_type = "UNKNOWN"
        nodes.append({
            "name": item["metadata"]["name"],
            "instance_type": instance_type,
            "capacity_type": capacity_type,
        })
    return nodes


def _take_node_snapshot(hour: int):
    """Record a node snapshot keyed by the JST hour label."""
    global _node_snapshots, _node_snap_last_taken
    now_jst = datetime.now(JST)
    date_str = now_jst.strftime("%Y-%m-%d")
    if _node_snap_last_taken.get(hour) == date_str:
        return
    try:
        nodes = _collect_node_data()
    except Exception:
        return
    label = f"{hour:02d}:00"
    snapshot = {
        "timestamp": now_jst.isoformat(),
        "nodes": nodes,
    }
    with _node_lock:
        _node_snapshots[label] = snapshot
        _node_snap_last_taken[hour] = date_str
        _save_node_snapshots()


def _node_scheduler():
    """Background thread: check every minute whether a node snapshot is due."""
    while True:
        time.sleep(60)
        now_jst = datetime.now(JST)
        if now_jst.hour in _NODE_SNAP_HOURS and now_jst.minute == 0:
            _take_node_snapshot(now_jst.hour)


threading.Thread(target=_node_scheduler, daemon=True).start()


def _run_cost_viewer():
    global _cost_status, _cost_log
    try:
        result = subprocess.run(
            [sys.executable, str(COST_VIEWER)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        with _cost_lock:
            if result.returncode == 0:
                _cost_status = "done"
                _cost_log = result.stdout
            else:
                _cost_status = "error"
                _cost_log = (result.stderr or result.stdout or "不明なエラー").strip()
    except subprocess.TimeoutExpired:
        with _cost_lock:
            _cost_status = "error"
            _cost_log = "タイムアウト (180s)"
    except Exception as exc:
        with _cost_lock:
            _cost_status = "error"
            _cost_log = str(exc)


def start_cost_viewer():
    """Start cost-viewer in background; no-op if already running."""
    global _cost_status, _cost_log
    with _cost_lock:
        if _cost_status == "running":
            return False
        _cost_status = "running"
        _cost_log = ""
    threading.Thread(target=_run_cost_viewer, daemon=True).start()
    return True


# ── Queue helpers ─────────────────────────────────────────────────────────────
def _collect_recovery_stats() -> dict:
    """Fetch stats/recovery-stats.json from S3 and aggregate."""
    try:
        r = subprocess.run(
            ["aws", "s3", "cp",
             f"s3://{S3_RECOVERY_BUCKET}/stats/recovery-stats.json", "-"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {"ok": True, "total_count": 0, "executions": 0}
        records = json.loads(r.stdout)
        total_count = sum(rec.get("count", 0) for rec in records)
        return {"ok": True, "total_count": total_count, "executions": len(records)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "total_count": 0, "executions": 0}


def _collect_s3_queue_counts() -> dict:
    """Use s3api list-objects-v2 with pagination to count eml files per pod."""
    try:
        pod_counts: dict = {}
        paginate_token = None
        while True:
            cmd = [
                "aws", "s3api", "list-objects-v2",
                "--bucket", "<YOUR_S3_RECOVERY_BUCKET>",
                "--prefix", "pending/",
                "--output", "json",
            ]
            if paginate_token:
                cmd += ["--continuation-token", paginate_token]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                break
            import json as _json
            data = _json.loads(r.stdout)
            for obj in data.get("Contents", []):
                if obj.get("Size", 0) == 0:
                    continue
                path_parts = obj["Key"].split("/")
                if len(path_parts) >= 3:
                    pod_name = path_parts[1]
                    pod_counts[pod_name] = pod_counts.get(pod_name, 0) + 1
            if data.get("IsTruncated"):
                paginate_token = data.get("NextContinuationToken")
            else:
                break
        return {"ok": True, "pods": pod_counts, "total": sum(pod_counts.values())}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "pods": {}, "total": 0}


def _collect_queue_counts() -> dict:
    """Run postqueue -p on every postfix pod and return {pod: count}.
    Count only lines starting with a queue ID ([A-F0-9]+) to get accurate
    per-mail counts (postqueue -p outputs 3-4 lines per message).
    Pod ごとの kubectl exec を ThreadPoolExecutor で並列実行する。
    """
    try:
        r = subprocess.run(
            ["kubectl", "get", "pods", "-l", "role=postfix",
             "-n", "default", "-o", 'jsonpath={range .items[*]}{.metadata.name}{"\\n"}{end}'],
            capture_output=True, text=True, timeout=60,
        )
        pod_names = [p.strip() for p in r.stdout.splitlines() if p.strip()]
    except Exception as exc:
        return {"error": str(exc)}

    def _fetch_one(pod: str):
        try:
            r2 = subprocess.run(
                ["kubectl", "exec", pod, "-n", "default", "--",
                 "sh", "-c", "postqueue -p 2>/dev/null | grep -c '^[A-F0-9]'"],
                capture_output=True, text=True, timeout=60,
            )
            raw = r2.stdout.strip()
            return pod, int(raw) if raw.isdigit() else 0
        except Exception as exc:
            return pod, f"error: {exc}"

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for pod, count in ex.map(_fetch_one, pod_names):
            results[pod] = count
    return results


def _take_queue_snapshot(hour: int):
    """Record a snapshot keyed by the JST hour label."""
    global _queue_snapshots, _queue_snap_last_taken
    now_jst = datetime.now(JST)
    date_str = now_jst.strftime("%Y-%m-%d")
    # Avoid duplicates within the same day
    if _queue_snap_last_taken.get(hour) == date_str:
        return
    counts = _collect_queue_counts()
    label = f"{hour:02d}:00"
    snapshot = {
        "timestamp": now_jst.isoformat(),
        "pods": counts,
    }
    with _queue_lock:
        _queue_snapshots[label] = snapshot
        _queue_snap_last_taken[hour] = date_str
        _save_queue_snapshots()


def _queue_scheduler():
    """Background thread: check every minute whether a snapshot is due."""
    while True:
        time.sleep(60)
        now_jst = datetime.now(JST)
        if now_jst.hour in _QUEUE_SNAP_HOURS and now_jst.minute == 0:
            _take_queue_snapshot(now_jst.hour)


threading.Thread(target=_queue_scheduler, daemon=True).start()


# ── Shared CSS / nav ─────────────────────────────────────────────────────────
_COMMON_CSS = """
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:      #0f172a;
      --surface: #1e293b;
      --border:  #334155;
      --muted:   #475569;
      --text:    #e2e8f0;
      --sub:     #94a3b8;
      --blue:    #38bdf8;
      --green:   #34d399;
      --red:     #f87171;
      --yellow:  #fbbf24;
      --radius:  10px;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   "Helvetica Neue", sans-serif;
      font-size: 14px;
      min-height: 100vh;
    }

    /* ── Header / Nav ── */
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0 1.5rem;
      height: 52px;
      display: flex;
      align-items: center;
      gap: 0.75rem;
      position: sticky;
      top: 0;
      z-index: 100;
    }
    .header-icon { font-size: 1.3rem; }
    header h1 { font-size: 1rem; font-weight: 600; color: var(--text); }

    nav {
      display: flex;
      align-items: center;
      gap: 0.25rem;
      margin-left: 1.5rem;
    }
    nav a {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0.3rem 0.75rem;
      border-radius: 6px;
      font-size: 0.82rem;
      font-weight: 500;
      color: var(--sub);
      text-decoration: none;
      transition: background 0.15s, color 0.15s;
    }
    nav a:hover { background: var(--border); color: var(--text); }
    nav a.active {
      background: #1c3052;
      color: var(--blue);
      border: 1px solid #2563eb44;
    }

    .header-badge {
      margin-left: auto;
      font-size: 0.72rem;
      color: var(--muted);
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 2px 8px;
    }

    /* ── Panel ── */
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .panel-head {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.85rem 1.1rem;
      border-bottom: 1px solid var(--border);
      font-weight: 600;
      font-size: 0.88rem;
      color: var(--sub);
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }
    .panel-head .icon { font-size: 1rem; }
    .panel-body { flex: 1; padding: 1.1rem; display: flex; flex-direction: column; gap: 1rem; }

    /* ── Buttons ── */
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.45rem 0.9rem;
      border-radius: 6px;
      border: 1px solid transparent;
      font-size: 0.82rem;
      font-weight: 500;
      cursor: pointer;
      transition: opacity 0.15s, background 0.15s;
      line-height: 1.4;
    }
    .btn:disabled { opacity: 0.45; cursor: not-allowed; }
    .btn-primary  { background: var(--blue);  color: #0f172a; border-color: var(--blue); }
    .btn-success  { background: var(--green); color: #0f172a; border-color: var(--green); }
    .btn-ghost    { background: transparent;  color: var(--sub); border-color: var(--border); }
    .btn-primary:hover:not(:disabled)  { opacity: 0.85; }
    .btn-success:hover:not(:disabled)  { opacity: 0.85; }
    .btn-ghost:hover:not(:disabled)    { background: var(--border); color: var(--text); }

    /* ── Status badge ── */
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      font-size: 0.75rem;
      padding: 2px 8px;
      border-radius: 20px;
      font-weight: 500;
    }
    .status-idle    { background: #1e293b; color: var(--muted); border: 1px solid var(--border); }
    .status-running { background: #1c2e4a; color: var(--blue);  border: 1px solid #2563eb44; }
    .status-done    { background: #14291f; color: var(--green); border: 1px solid #16a34a44; }
    .status-error   { background: #2d1515; color: var(--red);   border: 1px solid #dc262644; }
    .dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: currentColor;
    }
    .dot.pulse { animation: pulse 1.2s ease-in-out infinite; }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50%       { opacity: 0.3; }
    }

    /* ── Divider ── */
    .divider {
      height: 1px;
      background: var(--border);
      margin: 0.25rem 0;
    }
"""

_NAV_HTML = """
<header>
  <span class="header-icon">&#9881;</span>
  <h1>Mail Platform Admin</h1>
  <nav>
    <a href="/" class="{active_cost}">&#128200; AWS コスト</a>
    <a href="/helo" class="{active_helo}">&#9993; HELO 設定</a>
    <a href="/sender-canonical" class="{active_sc}">&#8644; バウンスドメイン設定</a>
    <a href="/queue" class="{active_queue}">&#128679; キュー監視</a>
    <a href="/metrics" class="{active_metrics}">&#128202; 監視</a>
    <a href="/cluster" class="{active_cluster}">&#127760; クラスタ構成</a>
    <a href="/cronjobs" class="{active_cron}">&#128337; CronJob 一覧</a>
    <a href="/node-monitor" class="{active_node}">&#128268; ノード種類</a>
  </nav>
  <span class="header-badge">mail-platform-eks</span>
</header>
"""


def _nav(page: str) -> str:
    return _NAV_HTML.format(
        active_cost="active" if page == "cost" else "",
        active_helo="active" if page == "helo" else "",
        active_sc="active" if page == "sc" else "",
        active_metrics="active" if page == "metrics" else "",
        active_queue="active" if page == "queue" else "",
        active_node="active" if page == "node" else "",
        active_cron="active" if page == "cron" else "",
        active_cluster="active" if page == "cluster" else "",
    )


# ── AWS Cost page ─────────────────────────────────────────────────────────────
def _cost_page() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AWS コスト – Mail Platform Admin</title>
  <style>
""" + _COMMON_CSS + """
    .main {
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 1rem;
      min-height: calc(100vh - 52px);
    }
    .cost-toolbar {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      flex-wrap: wrap;
    }
    #cost-log {
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
      font-size: 0.76rem;
      color: var(--sub);
      background: #0a111e;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.6rem 0.8rem;
      white-space: pre-wrap;
      max-height: 120px;
      overflow-y: auto;
      display: none;
    }
    .cost-iframe-wrap {
      flex: 1;
      border-radius: var(--radius);
      overflow: hidden;
      border: 1px solid var(--border);
      min-height: 600px;
    }
    #cost-iframe {
      width: 100%;
      height: 100%;
      min-height: 600px;
      border: none;
      display: block;
      background: var(--bg);
    }
  </style>
</head>
<body>
""" + _nav("cost") + """
<div class="main">
  <div class="panel">
    <div class="panel-head">
      <span class="icon">&#128200;</span>
      AWS コスト ダッシュボード
    </div>
    <div class="panel-body">
      <div class="cost-toolbar">
        <button class="btn btn-primary" id="cost-refresh-btn" onclick="refreshCost()">
          &#8635; 更新
        </button>
        <span class="status-badge status-idle" id="cost-status-badge">
          <span class="dot" id="cost-dot"></span>
          <span id="cost-status-text">待機中</span>
        </span>
      </div>
      <pre id="cost-log"></pre>
      <div class="cost-iframe-wrap">
        <iframe id="cost-iframe" src="/cost-report"></iframe>
      </div>
    </div>
  </div>
</div>

<script>
let costPoller = null;

async function refreshCost() {
  const btn = document.getElementById('cost-refresh-btn');
  btn.disabled = true;
  setStatus('running', '実行中…');
  showLog('');

  try {
    await fetch('/api/run-cost', { method: 'POST' });
    startCostPolling();
  } catch (e) {
    setStatus('error', 'エラー');
    showLog(String(e));
    btn.disabled = false;
  }
}

function startCostPolling() {
  if (costPoller) return;
  costPoller = setInterval(async () => {
    try {
      const res = await fetch('/api/cost-status');
      const data = await res.json();
      if (data.status === 'running') {
        setStatus('running', '実行中…');
      } else if (data.status === 'done') {
        setStatus('done', '完了');
        showLog(data.log);
        document.getElementById('cost-iframe').src = '/cost-report?t=' + Date.now();
        stopCostPolling();
        document.getElementById('cost-refresh-btn').disabled = false;
      } else if (data.status === 'error') {
        setStatus('error', 'エラー');
        showLog(data.log);
        stopCostPolling();
        document.getElementById('cost-refresh-btn').disabled = false;
      }
    } catch (e) { /* ignore transient */ }
  }, 2000);
}

function stopCostPolling() {
  if (costPoller) { clearInterval(costPoller); costPoller = null; }
}

function setStatus(state, text) {
  const badge = document.getElementById('cost-status-badge');
  const dot   = document.getElementById('cost-dot');
  const label = document.getElementById('cost-status-text');
  badge.className = 'status-badge status-' + state;
  dot.className   = 'dot' + (state === 'running' ? ' pulse' : '');
  label.textContent = text;
}

function showLog(text) {
  const el = document.getElementById('cost-log');
  if (text) { el.textContent = text; el.style.display = 'block'; }
  else       { el.style.display = 'none'; }
}

(async () => {
  try {
    const res = await fetch('/api/cost-status');
    const data = await res.json();
    if (data.status === 'running') {
      setStatus('running', '実行中…');
      startCostPolling();
    } else if (data.status === 'done') {
      setStatus('done', '完了');
    } else if (data.status === 'error') {
      setStatus('error', 'エラー');
      showLog(data.log);
    }
  } catch (e) { /* ignore */ }
})();
</script>
</body>
</html>
"""


# ── HELO page ─────────────────────────────────────────────────────────────────
def _helo_page() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>HELO 名設定 – Mail Platform Admin</title>
  <style>
""" + _COMMON_CSS + """
    .main {
      padding: 1.25rem;
      max-width: 860px;
      margin: 0 auto;
    }
    .helo-label {
      font-size: 0.75rem;
      color: var(--muted);
      letter-spacing: 0.04em;
      text-transform: uppercase;
      margin-bottom: 0.3rem;
    }
    #helo-textarea {
      width: 100%;
      min-height: 380px;
      background: #0a111e;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
      font-size: 0.8rem;
      line-height: 1.6;
      padding: 0.75rem;
      resize: vertical;
      outline: none;
      transition: border-color 0.15s;
    }
    #helo-textarea:focus { border-color: var(--blue); }
    .helo-actions {
      display: flex;
      gap: 0.6rem;
      align-items: center;
      flex-wrap: wrap;
    }
    #helo-result {
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
      font-size: 0.76rem;
      border-radius: 6px;
      padding: 0.6rem 0.8rem;
      white-space: pre-wrap;
      max-height: 200px;
      overflow-y: auto;
      display: none;
    }
    #helo-result.ok    { background: #14291f; color: var(--green); border: 1px solid #16a34a44; }
    #helo-result.error { background: #2d1515; color: var(--red);   border: 1px solid #dc262644; }
  </style>
</head>
<body>
""" + _nav("helo") + """
<div class="main">
  <div class="panel" style="margin-top:1.25rem">
    <div class="panel-head">
      <span class="icon">&#9993;</span>
      HELO 名設定
    </div>
    <div class="panel-body">
      <div>
        <div class="helo-label">configmap-helo.yaml</div>
        <textarea id="helo-textarea" spellcheck="false"></textarea>
      </div>
      <div class="helo-actions">
        <button class="btn btn-success" id="helo-apply-btn" onclick="applyHelo()">
          &#10003; Apply &amp; Rollout Restart
        </button>
        <button class="btn btn-ghost" onclick="reloadHelo()">
          &#8635; リセット
        </button>
        <span class="status-badge status-running" id="helo-status-badge" style="display:none">
          <span class="dot pulse"></span>
          <span>適用中…</span>
        </span>
      </div>
      <pre id="helo-result"></pre>
      <div class="divider"></div>
      <div style="color:var(--muted);font-size:0.75rem;line-height:1.6">
        Apply 時に実行されるコマンド:<br>
        <code style="color:var(--sub)">kubectl apply -f manifests/postfix/configmap-helo.yaml</code><br>
        <code style="color:var(--sub)">kubectl rollout restart deployment/postfix-deployment-a</code><br>
        <code style="color:var(--sub)">kubectl rollout restart deployment/postfix-deployment-b</code><br>
        <span style="color:var(--muted)">→ recovery-helo-script (kube-system) へ自動同期</span>
      </div>
    </div>
  </div>
</div>

<script>
async function reloadHelo() {
  try {
    const res = await fetch('/api/helo');
    const data = await res.json();
    if (data.ok) {
      document.getElementById('helo-textarea').value = data.content;
      hideHeloResult();
    } else {
      showHeloResult(false, 'ファイル読み込みエラー: ' + data.error);
    }
  } catch (e) {
    showHeloResult(false, String(e));
  }
}

async function applyHelo() {
  const content = document.getElementById('helo-textarea').value.trim();
  if (!content) { showHeloResult(false, 'YAML が空です'); return; }

  const btn   = document.getElementById('helo-apply-btn');
  const badge = document.getElementById('helo-status-badge');
  btn.disabled = true;
  badge.style.display = 'inline-flex';
  hideHeloResult();

  try {
    const res = await fetch('/api/helo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    });
    const data = await res.json();
    if (data.ok) {
      showHeloResult(true, data.output || '正常に適用されました');
    } else {
      showHeloResult(false, data.error || '不明なエラー');
    }
  } catch (e) {
    showHeloResult(false, String(e));
  } finally {
    btn.disabled = false;
    badge.style.display = 'none';
  }
}

function showHeloResult(ok, text) {
  const el = document.getElementById('helo-result');
  el.textContent = text;
  el.className = ok ? 'ok' : 'error';
  el.style.display = 'block';
}

function hideHeloResult() {
  const el = document.getElementById('helo-result');
  el.style.display = 'none';
  el.textContent = '';
}

reloadHelo();
</script>
</body>
</html>
"""


# ── Sender-canonical helpers ──────────────────────────────────────────────────

def _parse_sc_pairs(yaml_content: str) -> list:
    """Extract [{domain_b, domain_a}] from the configmap YAML content."""
    pairs = []
    for line in yaml_content.splitlines():
        m = _re.match(r'^\s*/\^\(\.\+\)@([^$]+)\$/\s+\$\{1\}@(.+?)\s*$', line)
        if m:
            pairs.append({"domain_b": m.group(1).strip(), "domain_a": m.group(2).strip()})
    return pairs


def _build_sc_yaml(pairs: list) -> str:
    """Generate configmap-recipient-canonical.yaml content from pairs list."""
    lines = []
    for p in pairs:
        db = p.get("domain_b", "").strip()
        da = p.get("domain_a", "").strip()
        if db and da:
            lines.append(f"    /^(.+)@{db}$/  ${{1}}@{da}")
    data_block = "\n".join(lines) + "\n" if lines else "\n"
    return (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: postfix-recipient-canonical\n"
        "  namespace: default\n"
        "data:\n"
        "  recipient_canonical: |\n"
        + data_block
    )


# ── Sender-canonical page ─────────────────────────────────────────────────────
def _sc_page() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>バウンスドメイン設定 – Mail Platform Admin</title>
  <style>
""" + _COMMON_CSS + """
    .main {
      padding: 1.25rem;
      max-width: 900px;
      margin: 0 auto;
    }
    .sc-table-head {
      display: grid;
      grid-template-columns: 1fr auto 1fr auto;
      gap: 0.6rem;
      align-items: center;
      padding: 0 0 0.4rem 0;
      font-size: 0.72rem;
      color: var(--muted);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .sc-row {
      display: grid;
      grid-template-columns: 1fr auto 1fr auto;
      gap: 0.6rem;
      align-items: center;
      padding: 0.35rem 0;
      border-bottom: 1px solid var(--border);
    }
    .sc-row:last-child { border-bottom: none; }
    .sc-input {
      width: 100%;
      background: #0a111e;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
      font-size: 0.8rem;
      padding: 0.4rem 0.6rem;
      outline: none;
      transition: border-color 0.15s;
    }
    .sc-input:focus { border-color: var(--blue); }
    .sc-arrow {
      color: var(--muted);
      font-size: 1rem;
      user-select: none;
    }
    .sc-rows-wrap {
      display: flex;
      flex-direction: column;
      min-height: 60px;
    }
    .sc-empty {
      color: var(--muted);
      font-size: 0.8rem;
      padding: 1rem 0;
      text-align: center;
    }
    .sc-actions {
      display: flex;
      gap: 0.6rem;
      align-items: center;
      flex-wrap: wrap;
    }
    #sc-result {
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
      font-size: 0.76rem;
      border-radius: 6px;
      padding: 0.6rem 0.8rem;
      white-space: pre-wrap;
      max-height: 200px;
      overflow-y: auto;
      display: none;
    }
    #sc-result.ok    { background: #14291f; color: var(--green); border: 1px solid #16a34a44; }
    #sc-result.error { background: #2d1515; color: var(--red);   border: 1px solid #dc262644; }
  </style>
</head>
<body>
""" + _nav("sc") + """
<div class="main">
  <div class="panel" style="margin-top:1.25rem">
    <div class="panel-head">
      <span class="icon">&#8644;</span>
      バウンスドメイン設定 (recipient_canonical)
    </div>
    <div class="panel-body">
      <div style="color:var(--sub);font-size:0.8rem;line-height:1.6">
        送信元ドメイン (ドメインB) を 書き換え後ドメイン (ドメインA) にマッピングします。<br>
        Postfix の <code style="color:var(--blue)">recipient_canonical_maps</code> (regexp) として適用されます。
      </div>
      <div>
        <div class="sc-table-head">
          <span>送信元ドメイン (ドメインB)</span>
          <span></span>
          <span>書き換え後ドメイン (ドメインA)</span>
          <span></span>
        </div>
        <div class="sc-rows-wrap" id="sc-rows">
          <div class="sc-empty" id="sc-empty">ペアがありません。「＋ 追加」で行を追加してください。</div>
        </div>
      </div>
      <div class="sc-actions">
        <button class="btn btn-ghost" onclick="addRow('','')">&#43; 追加</button>
        <button class="btn btn-success" id="sc-apply-btn" onclick="savePairs()">
          &#10003; 保存 &amp; Apply
        </button>
        <button class="btn btn-ghost" onclick="loadPairs()">&#8635; リセット</button>
        <span class="status-badge status-running" id="sc-status-badge" style="display:none">
          <span class="dot pulse"></span>
          <span>適用中…</span>
        </span>
      </div>
      <pre id="sc-result"></pre>
      <div class="divider"></div>
      <div style="color:var(--muted);font-size:0.75rem;line-height:1.6">
        保存時に実行されるコマンド:<br>
        <code style="color:var(--sub)">kubectl apply -f manifests/postfix/configmap-recipient-canonical.yaml</code><br>
        <code style="color:var(--sub)">kubectl rollout restart deployment/postfix-deployment-a</code><br>
        <code style="color:var(--sub)">kubectl rollout restart deployment/postfix-deployment-b</code><br>
        <span style="color:var(--muted)">→ recovery-recipient-canonical (kube-system) へ自動同期</span>
      </div>
    </div>
  </div>
</div>

<script>
function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function updateEmpty() {
  const rows = document.querySelectorAll('.sc-row');
  document.getElementById('sc-empty').style.display = rows.length ? 'none' : 'block';
}

function addRow(domainB, domainA) {
  const container = document.getElementById('sc-rows');
  const empty = document.getElementById('sc-empty');

  const row = document.createElement('div');
  row.className = 'sc-row';
  row.innerHTML =
    '<input class="sc-input" placeholder="example-b.com" value="' + escHtml(domainB) + '" />' +
    '<span class="sc-arrow">&#8644;</span>' +
    '<input class="sc-input" placeholder="example-a.com" value="' + escHtml(domainA) + '" />' +
    '<button class="btn btn-ghost" style="padding:0.3rem 0.6rem;font-size:0.75rem" onclick="removeRow(this)">&#10005;</button>';
  container.insertBefore(row, empty);
  updateEmpty();
}

function removeRow(btn) {
  btn.closest('.sc-row').remove();
  updateEmpty();
}

function collectPairs() {
  const pairs = [];
  document.querySelectorAll('.sc-row').forEach(row => {
    const inputs = row.querySelectorAll('input');
    const db = inputs[0].value.trim();
    const da = inputs[1].value.trim();
    if (db || da) pairs.push({ domain_b: db, domain_a: da });
  });
  return pairs;
}

async function loadPairs() {
  try {
    const res = await fetch('/api/sender-canonical');
    const data = await res.json();
    if (data.ok) {
      document.querySelectorAll('.sc-row').forEach(r => r.remove());
      updateEmpty();
      (data.pairs || []).forEach(p => addRow(p.domain_b || '', p.domain_a || ''));
      hideResult();
    } else {
      showResult(false, 'ロードエラー: ' + (data.error || '不明'));
    }
  } catch (e) {
    showResult(false, String(e));
  }
}

async function savePairs() {
  const pairs = collectPairs();
  const invalid = pairs.filter(p => !p.domain_b || !p.domain_a);
  if (invalid.length) {
    showResult(false, '空のドメインがあります。すべての行に送信元と書き換え後を入力してください。');
    return;
  }

  const btn   = document.getElementById('sc-apply-btn');
  const badge = document.getElementById('sc-status-badge');
  btn.disabled = true;
  badge.style.display = 'inline-flex';
  hideResult();

  try {
    const res = await fetch('/api/sender-canonical', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pairs }),
    });
    const data = await res.json();
    if (data.ok) {
      showResult(true, data.output || '正常に適用されました');
    } else {
      showResult(false, data.error || '不明なエラー');
    }
  } catch (e) {
    showResult(false, String(e));
  } finally {
    btn.disabled = false;
    badge.style.display = 'none';
  }
}

function showResult(ok, text) {
  const el = document.getElementById('sc-result');
  el.textContent = text;
  el.className = ok ? 'ok' : 'error';
  el.style.display = 'block';
}

function hideResult() {
  const el = document.getElementById('sc-result');
  el.style.display = 'none';
  el.textContent = '';
}

loadPairs();
</script>
</body>
</html>
"""


# ── Metrics page ─────────────────────────────────────────────────────────────
def _cron_page() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>CronJob 一覧 – Mail Platform Admin</title>
  <style>
""" + _COMMON_CSS + """
    .main {
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }
    .section-title {
      font-size: 0.72rem;
      font-weight: 600;
      color: var(--sub);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin-bottom: 0.6rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }
    th {
      text-align: left;
      padding: 0.45rem 0.75rem;
      background: var(--surface);
      color: var(--sub);
      font-weight: 600;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 0.55rem 0.75rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    tr:last-child td { border-bottom: none; }
    .name { font-family: "SF Mono", "Fira Code", Consolas, monospace; font-size: 0.8rem; }
    .schedule { font-family: "SF Mono", "Fira Code", Consolas, monospace; font-size: 0.78rem; color: var(--sub); }
    .badge {
      display: inline-block;
      padding: 1px 8px;
      border-radius: 10px;
      font-size: 0.68rem;
      font-weight: 600;
    }
    .badge-scale    { background: #1e3a5f; color: #7ec8f7; }
    .badge-queue    { background: #2d2a1a; color: #f0c040; }
    .badge-recovery { background: #1f2d1f; color: #7dd87d; }
    .badge-snap     { background: #1a2f1a; color: #6fcf6f; }
    .badge-watch    { background: #2a1a2a; color: #d09fdf; }
    .badge-thread   { background: #2a2a2a; color: #aaaaaa; }
    .note { font-size: 0.75rem; color: var(--muted); margin-top: 0.2rem; }
    .st-ok        { background: #1a3a2a; color: #6fcf6f; }
    .st-running   { background: #1e3a5f; color: #7ec8f7; }
    .st-failed    { background: #3a1a1a; color: #f07070; }
    .st-never     { background: #2a2a2a; color: #888888; }
    .st-suspended { background: #2a2a2a; color: #888888; }
    .st-loading   { background: #2a2a2a; color: #888888; }
    .ts { font-size: 0.72rem; color: var(--muted); }
    .refresh-note { font-size: 0.72rem; color: var(--muted); margin-bottom: 0.5rem; }
  </style>
</head>
<body>
""" + _nav("cron") + """
<div class="main">

  <div class="refresh-note" id="refresh-note">ステータスを取得中…</div>

  <div>
    <div class="section-title">&#9654; スケールコントロール</div>
    <table>
      <thead><tr><th>名前</th><th>スケジュール (JST)</th><th>内容</th><th>ステータス</th><th>最終実行</th><th>最終成功</th></tr></thead>
      <tbody>
        <tr data-cron="nightmode-on">
          <td><span class="name">nightmode-on</span></td>
          <td><span class="schedule">0 13 * * *</span><div class="note">22:00 JST</div></td>
          <td><span class="badge badge-scale">Scale</span> deployment-a/b → 各 8 replica</td>
          <td class="st-cell"><span class="badge st-loading">…</span></td>
          <td class="ts-sched ts"></td><td class="ts-success ts"></td>
        </tr>
        <tr data-cron="deepnight-on">
          <td><span class="name">deepnight-on</span></td>
          <td><span class="schedule">0 17 * * *</span><div class="note">02:00 JST</div></td>
          <td><span class="badge badge-scale">Scale</span> deployment-a/b → 各 4 replica</td>
          <td class="st-cell"><span class="badge st-loading">…</span></td>
          <td class="ts-sched ts"></td><td class="ts-success ts"></td>
        </tr>
        <tr data-cron="deepnight-off">
          <td><span class="name">deepnight-off</span></td>
          <td><span class="schedule">0 21 * * *</span><div class="note">06:00 JST</div></td>
          <td><span class="badge badge-scale">Scale</span> deployment-a/b → 各 8 replica</td>
          <td class="st-cell"><span class="badge st-loading">…</span></td>
          <td class="ts-sched ts"></td><td class="ts-success ts"></td>
        </tr>
        <tr data-cron="nightmode-off">
          <td><span class="name">nightmode-off</span></td>
          <td><span class="schedule">0 23 * * *</span><div class="note">08:00 JST</div></td>
          <td><span class="badge badge-scale">Scale</span> deployment-a/b → 各 35 replica</td>
          <td class="st-cell"><span class="badge st-loading">…</span></td>
          <td class="ts-sched ts"></td><td class="ts-success ts"></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div>
    <div class="section-title">&#9654; キュー救済</div>
    <table>
      <thead><tr><th>名前</th><th>スケジュール (JST)</th><th>内容</th><th>ステータス</th><th>最終実行</th><th>最終成功</th></tr></thead>
      <tbody>
        <tr data-cron="queue-monitor">
          <td><span class="name">queue-monitor</span></td>
          <td><span class="schedule">*/2 * * * *</span><div class="note">2 分ごと</div></td>
          <td><span class="badge badge-queue">Queue</span> キュー 1000通超 Pod の mail を S3 保存 → ノード削除</td>
          <td class="st-cell"><span class="badge st-loading">…</span></td>
          <td class="ts-sched ts"></td><td class="ts-success ts"></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div>
    <div class="section-title">&#9654; S3 リカバリー</div>
    <table>
      <thead><tr><th>名前</th><th>スケジュール (JST)</th><th>内容</th><th>ステータス</th><th>最終実行</th><th>最終成功</th></tr></thead>
      <tbody>
        <tr data-cron="s3-recovery">
          <td><span class="name">s3-recovery</span></td>
          <td><span class="schedule">0 0,2,4,6,8,10 * * *</span><div class="note">9/11/13/15/17/19時 (2時間おき)</div></td>
          <td><span class="badge badge-recovery">Recovery</span> S3 退避メールを 5 Pod 並列で再送 (JST 9〜19時)</td>
          <td class="st-cell"><span class="badge st-loading">…</span></td>
          <td class="ts-sched ts"></td><td class="ts-success ts"></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div>
    <div class="section-title">&#9654; 監視・通知</div>
    <table>
      <thead><tr><th>名前</th><th>スケジュール (JST)</th><th>内容</th><th>ステータス</th><th>最終実行</th><th>最終成功</th></tr></thead>
      <tbody>
        <tr data-cron="karpenter-watch">
          <td><span class="name">karpenter-watch</span></td>
          <td><span class="schedule">*/5 * * * *</span><div class="note">5 分ごと</div></td>
          <td><span class="badge badge-watch">Watch</span> Karpenter Pod 異常検知 → ChatWork 通知</td>
          <td class="st-cell"><span class="badge st-loading">…</span></td>
          <td class="ts-sched ts"></td><td class="ts-success ts"></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div>
    <div class="section-title">&#9654; admin-server 内部スレッド（CronJob 外）</div>
    <table>
      <thead><tr><th>名前</th><th>スケジュール (JST)</th><th>内容</th></tr></thead>
      <tbody>
        <tr>
          <td><span class="name">_queue_scheduler</span></td>
          <td><span class="schedule">00:00 / 03:00 / 06:00</span></td>
          <td><span class="badge badge-thread">Thread</span> mailq スナップショット取得（1日1回）</td>
        </tr>
        <tr>
          <td><span class="name">_node_scheduler</span></td>
          <td><span class="schedule">23:00 / 03:00 / 07:00</span></td>
          <td><span class="badge badge-thread">Thread</span> ノードスナップショット取得</td>
        </tr>
      </tbody>
    </table>
  </div>

</div>
<script>
  function fmtTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    const now = new Date();
    const diffMin = Math.round((now - d) / 60000);
    const ts = d.toLocaleString('ja-JP', {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'});
    const ago = diffMin < 60 ? diffMin + '分前' : Math.round(diffMin/60) + '時間前';
    return ts + ' (' + ago + ')';
  }

  const BADGE_LABEL = { ok:'OK', running:'実行中', failed:'FAILED', never:'未実行', suspended:'SUSPENDED' };

  function refresh() {
    fetch('/api/cronjob-status')
      .then(r => r.json())
      .then(data => {
        if (!data.ok) {
          document.getElementById('refresh-note').textContent = 'エラー: ' + data.error;
          return;
        }
        const map = {};
        data.items.forEach(item => { map[item.name] = item; });

        document.querySelectorAll('tr[data-cron]').forEach(row => {
          const name = row.dataset.cron;
          const item = map[name];
          if (!item) return;
          row.querySelector('.st-cell').innerHTML =
            '<span class="badge st-' + item.badge + '">' + (BADGE_LABEL[item.badge] || item.badge) + '</span>';
          row.querySelector('.ts-sched').textContent   = fmtTime(item.lastScheduleTime);
          row.querySelector('.ts-success').textContent = fmtTime(item.lastSuccessfulTime);
        });

        const now = new Date().toLocaleTimeString('ja-JP');
        document.getElementById('refresh-note').textContent = '最終取得: ' + now + '　(30秒ごと自動更新)';
      })
      .catch(e => {
        document.getElementById('refresh-note').textContent = '取得失敗: ' + e;
      });
  }

  refresh();
  setInterval(refresh, 30000);
</script>
</body>
</html>
"""


def _cluster_page() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>クラスタ構成 – Mail Platform Admin</title>
  <style>
""" + _COMMON_CSS + """
    .main {
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }
    .section-title {
      font-size: 0.72rem;
      font-weight: 600;
      color: var(--sub);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin-bottom: 0.6rem;
    }
    .ns-note {
      font-weight: 400;
      text-transform: none;
      letter-spacing: 0;
      color: var(--muted);
      margin-left: 0.5rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
    }
    th {
      text-align: left;
      padding: 0.45rem 0.75rem;
      background: var(--surface);
      color: var(--sub);
      font-weight: 600;
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      border-bottom: 1px solid var(--border);
    }
    td {
      padding: 0.55rem 0.75rem;
      border-bottom: 1px solid var(--border);
      vertical-align: top;
    }
    tr:last-child td { border-bottom: none; }
    .name { font-family: "SF Mono", "Fira Code", Consolas, monospace; font-size: 0.8rem; }
    .note { font-size: 0.75rem; color: var(--muted); margin-top: 0.2rem; }
  </style>
</head>
<body>
""" + _nav("cluster") + """
<div class="main">

  <div>
    <div class="section-title">&#9654; default <span class="ns-note">アプリケーション本体</span></div>
    <table>
      <thead><tr><th>リソース</th><th>種別</th><th>備考</th></tr></thead>
      <tbody>
        <tr>
          <td><span class="name">postfix-deployment-a</span></td>
          <td>Deployment</td>
          <td>配信ワーカー<div class="note">expireAfter: 40分 / nodepool-a-*</div></td>
        </tr>
        <tr>
          <td><span class="name">postfix-deployment-b</span></td>
          <td>Deployment</td>
          <td>配信ワーカー<div class="note">expireAfter: 50分 / nodepool-b-*</div></td>
        </tr>
        <tr>
          <td><span class="name">gateway</span></td>
          <td>DaemonSet</td>
          <td>HAProxy（オンプレ橋渡し）<div class="note">hostNetwork: true / On-Demand 固定</div></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div>
    <div class="section-title">&#9654; kube-system <span class="ns-note">K8s 基盤 + 運用系</span></div>
    <table>
      <thead><tr><th>リソース</th><th>種別</th><th>備考</th></tr></thead>
      <tbody>
        <tr>
          <td><span class="name">nightmode-on / off</span></td>
          <td>CronJob</td>
          <td>夜間スケールダウン（22:00 / 08:00 JST）</td>
        </tr>
        <tr>
          <td><span class="name">deepnight-on / off</span></td>
          <td>CronJob</td>
          <td>深夜スケールダウン（02:00 / 06:00 JST）</td>
        </tr>
        <tr>
          <td><span class="name">queue-monitor</span></td>
          <td>CronJob</td>
          <td>キュー1000件超 → S3退避 + ノード削除（2分ごと）</td>
        </tr>
        <tr>
          <td><span class="name">s3-recovery</span></td>
          <td>CronJob</td>
          <td>S3退避メール再送（JST 9〜19時・2時間おき）</td>
        </tr>
        <tr>
          <td><span class="name">karpenter-watch</span></td>
          <td>CronJob</td>
          <td>Karpenter 異常検知 → ChatWork 通知（5分ごと）</td>
        </tr>
        <tr>
          <td><span class="name">aws-node</span></td>
          <td>DaemonSet</td>
          <td>VPC CNI（各ノードに1つ）</td>
        </tr>
        <tr>
          <td><span class="name">kube-proxy</span></td>
          <td>DaemonSet</td>
          <td>K8s 標準（各ノードに1つ）</td>
        </tr>
        <tr>
          <td><span class="name">coredns</span></td>
          <td>Deployment</td>
          <td>DNS 解決</td>
        </tr>
        <tr>
          <td><span class="name">metrics-server</span></td>
          <td>Deployment</td>
          <td>メトリクス収集</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div>
    <div class="section-title">&#9654; karpenter <span class="ns-note">ノード自動管理</span></div>
    <table>
      <thead><tr><th>リソース</th><th>種別</th><th>備考</th></tr></thead>
      <tbody>
        <tr>
          <td><span class="name">karpenter</span></td>
          <td>Deployment</td>
          <td>ノードのオートスケール・置き換えを管理</td>
        </tr>
      </tbody>
    </table>
  </div>

</div>
</body>
</html>
"""


def _metrics_page() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>監視 – Mail Platform Admin</title>
  <style>
""" + _COMMON_CSS + """
    .main {
      padding: 1.25rem;
      display: flex;
      flex-direction: column;
      gap: 1.25rem;
      min-height: calc(100vh - 52px);
    }
    /* ── Toolbar ── */
    .metrics-toolbar {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      flex-wrap: wrap;
    }
    .timestamp { font-size: 0.72rem; color: var(--muted); }

    /* ── Section header ── */
    .section-header {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      margin-bottom: 0.75rem;
      font-size: 0.76rem;
      font-weight: 600;
      color: var(--sub);
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .count-badge {
      background: var(--border);
      color: var(--text);
      border-radius: 10px;
      padding: 1px 8px;
      font-size: 0.69rem;
      font-weight: 500;
    }

    /* ── Node cards ── */
    .node-cards {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 0.75rem;
    }
    .node-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 0.9rem 1rem;
    }
    .node-card-name {
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
      font-size: 0.77rem;
      color: var(--blue);
      margin-bottom: 0.7rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* ── Metric bar row ── */
    .metric-row {
      display: flex;
      flex-direction: column;
      gap: 0.22rem;
      margin-bottom: 0.55rem;
    }
    .metric-row:last-child { margin-bottom: 0; }
    .metric-label {
      display: flex;
      justify-content: space-between;
      font-size: 0.69rem;
      color: var(--sub);
    }
    .metric-value { color: var(--text); font-weight: 500; }
    .bar-track {
      height: 5px;
      background: var(--border);
      border-radius: 3px;
      overflow: hidden;
    }
    .bar-fill {
      height: 100%;
      border-radius: 3px;
      transition: width 0.45s ease;
      min-width: 2px;
    }
    .bar-cpu  { background: var(--blue); }
    .bar-mem  { background: var(--green); }
    .bar-warn { background: var(--yellow); }
    .bar-crit { background: var(--red); }

    /* ── Pod group ── */
    .pod-group {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      overflow: hidden;
    }
    .pod-group-head {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.65rem 1rem;
      background: #0a111e;
      border-bottom: 1px solid var(--border);
      cursor: pointer;
      user-select: none;
    }
    .pod-group-head:hover { background: #111827; }
    .pod-group-title { font-size: 0.82rem; font-weight: 600; color: var(--text); }
    .pod-group-badge {
      background: var(--border);
      color: var(--sub);
      border-radius: 10px;
      padding: 1px 8px;
      font-size: 0.68rem;
    }
    .chevron {
      margin-left: auto;
      font-size: 0.65rem;
      color: var(--muted);
      transition: transform 0.2s;
    }
    .chevron.open { transform: rotate(180deg); }

    /* ── Pod cards grid ── */
    .pod-cards-wrap { padding: 0.85rem; }
    .pod-cards {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 0.65rem;
    }
    .pod-card {
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.75rem;
    }
    .pod-card-header {
      display: flex;
      align-items: flex-start;
      gap: 0.4rem;
      margin-bottom: 0.55rem;
    }
    .pod-card-name {
      flex: 1;
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
      font-size: 0.71rem;
      color: var(--blue);
      word-break: break-all;
      line-height: 1.4;
    }
    .pod-status-pill {
      flex-shrink: 0;
      font-size: 0.63rem;
      font-weight: 600;
      padding: 1px 6px;
      border-radius: 10px;
      text-transform: uppercase;
    }
    .pill-running { background: #14291f; color: var(--green);  border: 1px solid #16a34a44; }
    .pill-pending { background: #2d2008; color: var(--yellow); border: 1px solid #b4530044; }
    .pill-error   { background: #2d1515; color: var(--red);    border: 1px solid #dc262644; }
    .pill-unknown { background: #1e293b; color: var(--sub);    border: 1px solid var(--border); }
    .pod-meta {
      display: flex;
      gap: 0.55rem;
      flex-wrap: wrap;
      margin-bottom: 0.5rem;
      font-size: 0.66rem;
      color: var(--muted);
    }
    .pod-node-ip {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      font-size: 0.66rem;
      color: var(--sub);
      background: #0a111e;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 1px 6px;
      margin-bottom: 0.45rem;
      font-family: "SF Mono", "Fira Code", Consolas, monospace;
    }
    .pod-node-ip .node-ip-label { color: var(--muted); }
    .empty-state {
      text-align: center;
      color: var(--muted);
      font-size: 0.8rem;
      padding: 2rem;
    }
  </style>
</head>
<body>
""" + _nav("metrics") + """
<div class="main">

  <!-- ── Header toolbar ── -->
  <div class="panel">
    <div class="panel-head">
      <span class="icon">&#128202;</span>
      クラスター監視
      <div class="metrics-toolbar" style="margin-left:auto">
        <span class="timestamp" id="metrics-timestamp"></span>
        <button class="btn btn-primary" id="metrics-refresh-btn" onclick="refreshMetrics()">
          &#8635; 更新
        </button>
        <span class="status-badge status-idle" id="metrics-status-badge">
          <span class="dot" id="metrics-dot"></span>
          <span id="metrics-status-text">待機中</span>
        </span>
      </div>
    </div>
  </div>

  <!-- ── Node section ── -->
  <div>
    <div class="section-header">
      <span>&#128736;</span> Nodes
      <span class="count-badge" id="nodes-count">–</span>
    </div>
    <div class="node-cards" id="node-cards">
      <div class="empty-state" style="grid-column:1/-1">更新ボタンを押してください</div>
    </div>
  </div>

  <!-- ── Pod groups ── -->
  <div>
    <div class="section-header">
      <span>&#128230;</span> Pods
      <span class="count-badge" id="pods-count">–</span>
    </div>
    <div id="pod-groups" style="display:flex;flex-direction:column;gap:0.65rem">
      <div class="empty-state">更新ボタンを押してください</div>
    </div>
  </div>

</div><!-- /main -->

<script>
// ── Value parsers ────────────────────────────────────────────────────────────
function parseCpuMilli(s) {
  if (!s || s === '<unknown>') return null;
  s = s.trim();
  if (s.endsWith('m')) return parseInt(s) || 0;
  return Math.round(parseFloat(s) * 1000) || 0;
}

function parseMemMi(s) {
  if (!s || s === '<unknown>') return null;
  s = s.trim();
  if (s.endsWith('Ki')) return Math.round(parseInt(s) / 1024);
  if (s.endsWith('Mi')) return parseInt(s);
  if (s.endsWith('Gi')) return Math.round(parseFloat(s) * 1024);
  if (s.endsWith('Ti')) return Math.round(parseFloat(s) * 1024 * 1024);
  return Math.round(parseInt(s) / (1024 * 1024));
}

function fmtCpu(m) {
  if (m === null || m === undefined) return '–';
  if (m >= 1000) return (m / 1000).toFixed(2) + ' cores';
  return m + 'm';
}

function fmtMem(mi) {
  if (mi === null || mi === undefined) return '–';
  if (mi >= 1024) return (mi / 1024).toFixed(1) + ' Gi';
  return mi + ' Mi';
}

// ── Pod group classifier ─────────────────────────────────────────────────────
function classifyPod(name) {
  const n = name.toLowerCase();
  if (/postfix/.test(n))              return 'postfix';
  if (/gateway|nginx|ingress/.test(n)) return 'gateway';
  return 'system';
}

const GROUP_META = {
  postfix: { icon: '&#9993;',   label: 'Postfix'  },
  gateway: { icon: '&#127760;', label: 'Gateway'  },
  system:  { icon: '&#9881;',   label: 'System'   },
};

// ── Instance type → memory capacity ──────────────────────────────────────────
const INSTANCE_MEMORY_GB = {
  // t2
  't2.nano': 0.5, 't2.micro': 1, 't2.small': 2, 't2.medium': 4,
  't2.large': 8, 't2.xlarge': 16, 't2.2xlarge': 32,
  // t3
  't3.nano': 0.5, 't3.micro': 1, 't3.small': 2, 't3.medium': 4,
  't3.large': 8, 't3.xlarge': 16, 't3.2xlarge': 32,
  // t3a
  't3a.nano': 0.5, 't3a.micro': 1, 't3a.small': 2, 't3a.medium': 4,
  't3a.large': 8, 't3a.xlarge': 16, 't3a.2xlarge': 32,
  // m5
  'm5.large': 8, 'm5.xlarge': 16, 'm5.2xlarge': 32, 'm5.4xlarge': 64,
  'm5.8xlarge': 128, 'm5.12xlarge': 192, 'm5.16xlarge': 256, 'm5.24xlarge': 384,
  // m5a
  'm5a.large': 8, 'm5a.xlarge': 16, 'm5a.2xlarge': 32, 'm5a.4xlarge': 64,
  // m6i
  'm6i.large': 8, 'm6i.xlarge': 16, 'm6i.2xlarge': 32, 'm6i.4xlarge': 64,
  'm6i.8xlarge': 128, 'm6i.12xlarge': 192, 'm6i.16xlarge': 256,
  // m6a
  'm6a.large': 8, 'm6a.xlarge': 16, 'm6a.2xlarge': 32, 'm6a.4xlarge': 64,
  // c5
  'c5.large': 4, 'c5.xlarge': 8, 'c5.2xlarge': 16, 'c5.4xlarge': 32,
  'c5.9xlarge': 72, 'c5.12xlarge': 96, 'c5.18xlarge': 144, 'c5.24xlarge': 192,
  // c6i
  'c6i.large': 4, 'c6i.xlarge': 8, 'c6i.2xlarge': 16, 'c6i.4xlarge': 32,
  // r5
  'r5.large': 16, 'r5.xlarge': 32, 'r5.2xlarge': 64, 'r5.4xlarge': 128,
  // r6i
  'r6i.large': 16, 'r6i.xlarge': 32, 'r6i.2xlarge': 64, 'r6i.4xlarge': 128,
};

function instanceMemoryLabel(itype) {
  if (!itype) return '';
  const gb = INSTANCE_MEMORY_GB[itype];
  if (gb === undefined) return itype;
  const label = gb < 1 ? (gb * 1024) + 'MB' : gb + 'GB';
  return `${itype} &nbsp;<span style="color:var(--muted);font-size:.75em">${label}</span>`;
}

// ── Render: node cards ───────────────────────────────────────────────────────
function renderNodes(rows, nodeIps, gatewayNodes, nodeInstanceTypes) {
  const container = document.getElementById('node-cards');
  document.getElementById('nodes-count').textContent = rows.length;
  if (!rows.length) {
    container.innerHTML = '<div class="empty-state" style="grid-column:1/-1">データなし</div>';
    return;
  }
  nodeIps           = nodeIps           || {};
  gatewayNodes      = gatewayNodes      || [];
  nodeInstanceTypes = nodeInstanceTypes || {};
  const gwSet  = new Set(gatewayNodes);

  // Sort: gateway nodes first, then the rest
  const sorted = [...rows].sort((a, b) => {
    const aGw = gwSet.has(a[0]) ? 0 : 1;
    const bGw = gwSet.has(b[0]) ? 0 : 1;
    return aGw - bGw;
  });

  // rows: [NAME, CPU(cores), CPU%, MEMORY(bytes), MEMORY%]
  let gwIndex = 0;
  container.innerHTML = sorted.map(r => {
    const cpuPct  = parseInt(r[2]) || 0;
    const memPct  = parseInt(r[4]) || 0;
    const cpuCol  = cpuPct >= 85 ? 'bar-crit' : cpuPct >= 65 ? 'bar-warn' : 'bar-cpu';
    const memCol  = memPct >= 85 ? 'bar-crit' : memPct >= 65 ? 'bar-warn' : 'bar-mem';
    const isGw    = gwSet.has(r[0]);
    const extIp   = nodeIps[r[0]] || '';
    const itype   = nodeInstanceTypes[r[0]] || '';
    const title   = isGw
      ? `&#127760; Gateway${extIp ? ' (' + escHtml(extIp) + ')' : ''}`
      : `&#9679; ${escHtml(r[0])}`;
    const cardStyle = isGw
      ? ` style="border-color:#2563eb88;background:linear-gradient(135deg,#1e293b,#1c3052);grid-column:${++gwIndex};grid-row:1"`
      : '';
    const itypeHtml = itype
      ? `<div style="font-size:.78em;margin-bottom:.35em;color:#94a3b8">&#128190; ${instanceMemoryLabel(itype)}</div>`
      : '';
    return `
    <div class="node-card"${cardStyle}>
      <div class="node-card-name">${title}</div>
      ${itypeHtml}<div class="metric-row">
        <div class="metric-label">
          <span>CPU</span>
          <span class="metric-value">${escHtml(r[1])} &nbsp;<strong>${cpuPct}%</strong></span>
        </div>
        <div class="bar-track">
          <div class="bar-fill ${cpuCol}" style="width:${Math.min(cpuPct,100)}%"></div>
        </div>
      </div>
      <div class="metric-row">
        <div class="metric-label">
          <span>Memory</span>
          <span class="metric-value">${escHtml(r[3])}${(() => { const gb = INSTANCE_MEMORY_GB[itype]; return gb !== undefined ? ` / ${gb < 1 ? (gb * 1024) + 'MB' : gb + 'GB'}` : ''; })()} &nbsp;<strong>${memPct}%</strong></span>
        </div>
        <div class="bar-track">
          <div class="bar-fill ${memCol}" style="width:${Math.min(memPct,100)}%"></div>
        </div>
      </div>
    </div>`;
  }).join('');
}

// ── Render: pod groups ───────────────────────────────────────────────────────
function renderPodGroups(topPods, getPods, nodeIps) {
  // Build name → status info lookup from get_pods
  // get_pods rows: [NAME, READY, STATUS, RESTARTS, AGE, IP, NODE]
  const info = {};
  for (const r of (getPods || [])) {
    info[r[0]] = { ready: r[1], status: r[2], restarts: r[3], age: r[4], node: r[6] };
  }
  nodeIps = nodeIps || {};

  // Build merged pod list per group
  const groups = { postfix: [], gateway: [], system: [] };

  // Seed from top_pods (have CPU/mem data)
  for (const r of (topPods || [])) {
    const name = r[0];
    const d    = info[name] || {};
    groups[classifyPod(name)].push({
      name, cpuM: parseCpuMilli(r[1]), memMi: parseMemMi(r[2]),
      cpuRaw: r[1], memRaw: r[2], ...d,
    });
  }

  // Add pods present only in get_pods (no metrics yet)
  for (const r of (getPods || [])) {
    const name = r[0];
    const grp  = classifyPod(name);
    if (!groups[grp].find(p => p.name === name)) {
      groups[grp].push({ name, cpuM: null, memMi: null,
        ready: r[1], status: r[2], restarts: r[3], age: r[4], node: r[6] });
    }
  }

  const total = Object.values(groups).reduce((s, g) => s + g.length, 0);
  document.getElementById('pods-count').textContent = total;

  document.getElementById('pod-groups').innerHTML =
    ['postfix', 'gateway', 'system'].map(key => {
      const pods = groups[key];
      const meta = GROUP_META[key];

      // Fixed scale: t3.small capacity (1 vCPU = 1000m, 2 GB = 2048 Mi)
      const maxCpu = 1000;
      const maxMem = 2048;

      const cardsHtml = pods.length === 0
        ? '<div class="empty-state">該当 Pod なし</div>'
        : pods.map(p => {
            const cpuPct = p.cpuM  !== null ? Math.round(p.cpuM  / maxCpu * 100) : 0;
            const memPct = p.memMi !== null ? Math.round(p.memMi / maxMem * 100) : 0;
            const status = p.status || '';
            const pillCls = status === 'Running' ? 'pill-running'
                          : status === 'Pending' ? 'pill-pending'
                          : status               ? 'pill-error'
                          :                        'pill-unknown';
            const nodeExtIp = p.node ? (nodeIps[p.node] || '') : '';
            return `
            <div class="pod-card">
              <div class="pod-card-header">
                <div class="pod-card-name">${escHtml(p.name)}</div>
                ${status ? `<span class="pod-status-pill ${pillCls}">${escHtml(status)}</span>` : ''}
              </div>
              ${nodeExtIp ? `<div class="pod-node-ip"><span class="node-ip-label">&#127760;</span> ${escHtml(nodeExtIp)}</div>` : ''}
              <div class="pod-meta">
                ${p.ready    !== undefined ? `<span>Ready: ${escHtml(p.ready||'–')}</span>` : ''}
                ${p.restarts !== undefined ? `<span>Restarts: ${escHtml(p.restarts||'0')}</span>` : ''}
                ${p.age      ? `<span>Age: ${escHtml(p.age)}</span>` : ''}
              </div>
              <div class="metric-row">
                <div class="metric-label">
                  <span>CPU</span>
                  <span class="metric-value">${escHtml(fmtCpu(p.cpuM))}</span>
                </div>
                <div class="bar-track">
                  <div class="bar-fill bar-cpu" style="width:${cpuPct}%"></div>
                </div>
              </div>
              <div class="metric-row">
                <div class="metric-label">
                  <span>Memory</span>
                  <span class="metric-value">${escHtml(fmtMem(p.memMi))}</span>
                </div>
                <div class="bar-track">
                  <div class="bar-fill bar-mem" style="width:${memPct}%"></div>
                </div>
              </div>
            </div>`;
          }).join('');

      return `
      <div class="pod-group">
        <div class="pod-group-head" onclick="toggleGroup(this)">
          <span>${meta.icon}</span>
          <span class="pod-group-title">${meta.label}</span>
          <span class="pod-group-badge">${pods.length} pods</span>
          <span class="chevron open">&#9660;</span>
        </div>
        <div class="pod-cards-wrap">
          <div class="pod-cards">${cardsHtml}</div>
        </div>
      </div>`;
    }).join('');
}

function toggleGroup(head) {
  const wrap    = head.nextElementSibling;
  const chevron = head.querySelector('.chevron');
  const isOpen  = wrap.style.display !== 'none';
  wrap.style.display = isOpen ? 'none' : '';
  chevron.classList.toggle('open', !isOpen);
}

// ── Main refresh ─────────────────────────────────────────────────────────────
async function refreshMetrics() {
  const btn = document.getElementById('metrics-refresh-btn');
  btn.disabled = true;
  setMetricsStatus('running', '取得中…');
  try {
    const res  = await fetch('/api/metrics');
    const data = await res.json();
    if (data.ok) {
      renderNodes(data.top_nodes || [], data.node_ips || {}, data.gateway_nodes || [], data.node_instance_types || {});
      renderPodGroups(data.top_pods || [], data.get_pods || [], data.node_ips || {});
      setMetricsStatus('done', '完了');
      document.getElementById('metrics-timestamp').textContent =
        '最終更新: ' + new Date().toLocaleTimeString('ja-JP');
    } else {
      setMetricsStatus('error', 'エラー');
      document.getElementById('node-cards').innerHTML =
        `<div class="empty-state" style="color:var(--red);grid-column:1/-1">${escHtml(data.error||'取得失敗')}</div>`;
      document.getElementById('pod-groups').innerHTML =
        `<div class="empty-state" style="color:var(--red)">${escHtml(data.error||'取得失敗')}</div>`;
    }
  } catch (e) {
    setMetricsStatus('error', 'エラー');
  } finally {
    btn.disabled = false;
  }
}

function setMetricsStatus(state, text) {
  const badge = document.getElementById('metrics-status-badge');
  const dot   = document.getElementById('metrics-dot');
  const label = document.getElementById('metrics-status-text');
  badge.className  = 'status-badge status-' + state;
  dot.className    = 'dot' + (state === 'running' ? ' pulse' : '');
  label.textContent = text;
}

function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
</script>
</body>
</html>
"""


# ── HTTP Request Handler ──────────────────────────────────────────────────────
class AdminHandler(BaseHTTPRequestHandler):

    # ── Auth ──────────────────────────────────────────────────────────────────
    def _check_auth(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
                u, _, p = decoded.partition(":")
                if u == USERNAME and p == PASSWORD:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Mail Platform Admin"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "12")
        self.end_headers()
        self.wfile.write(b"Unauthorized")
        return False

    # ── Routing ───────────────────────────────────────────────────────────────
    def do_GET(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", _cost_page().encode("utf-8"))
        elif path == "/helo":
            self._send(200, "text/html; charset=utf-8", _helo_page().encode("utf-8"))
        elif path == "/cost-report":
            self._serve_cost_report()
        elif path == "/api/cost-status":
            self._api_cost_status()
        elif path == "/api/helo":
            self._api_get_helo()
        elif path == "/sender-canonical":
            self._send(200, "text/html; charset=utf-8", _sc_page().encode("utf-8"))
        elif path == "/api/sender-canonical":
            self._api_get_sc()
        elif path == "/metrics":
            self._send(200, "text/html; charset=utf-8", _metrics_page().encode("utf-8"))
        elif path == "/api/metrics":
            self._api_get_metrics()
        elif path == "/queue":
            self._serve_static_html("queue-monitor.html")
        elif path == "/node-monitor":
            self._serve_static_html("node-monitor.html")
        elif path == "/cronjobs":
            self._send(200, "text/html; charset=utf-8", _cron_page().encode("utf-8"))
        elif path == "/api/cronjob-status":
            self._api_get_cronjob_status()
        elif path == "/cluster":
            self._send(200, "text/html; charset=utf-8", _cluster_page().encode("utf-8"))
        elif path == "/api/queue":
            self._api_get_queue()
        elif path == "/api/s3-queue-count":
            self._api_get_s3_queue_count()
        elif path == "/api/recovery-stats":
            self._api_get_recovery_stats()
        elif path == "/api/node-snapshots":
            self._api_get_node_snapshots()
        else:
            self._send(404, "text/plain", b"Not Found")


    def do_POST(self):
        if not self._check_auth():
            return
        path = urlparse(self.path).path
        if path == "/api/run-cost":
            start_cost_viewer()
            self._json({"ok": True})
        elif path == "/api/helo":
            self._api_post_helo()
        elif path == "/api/sender-canonical":
            self._api_post_sc()
        elif path == "/api/queue/snapshot":
            self._api_post_queue_snapshot()
        elif path == "/api/node-snapshot":
            self._api_post_node_snapshot()
        elif path == "/api/node-snapshot/fetch":
            self._api_post_node_snapshot_fetch()
        else:
            self._send(404, "text/plain", b"Not Found")

    # ── Handlers ──────────────────────────────────────────────────────────────
    def _serve_cost_report(self):
        if REPORT_HTML.exists():
            content = REPORT_HTML.read_bytes()
        else:
            content = (
                "<html><body style='"
                "background:#0f172a;color:#94a3b8;"
                "font-family:sans-serif;padding:3rem;font-size:14px"
                "'>"
                "<p>&#128202; レポートがまだ生成されていません。<br>"
                "「更新」ボタンを押してコストデータを取得してください。</p>"
                "</body></html>"
            ).encode("utf-8")
        self._send(200, "text/html; charset=utf-8", content)

    def _api_cost_status(self):
        with _cost_lock:
            self._json({"status": _cost_status, "log": _cost_log})

    def _api_get_cronjob_status(self):
        import json as _json
        try:
            r = subprocess.run(
                ["kubectl", "get", "cronjobs", "--all-namespaces", "-o", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                self._json({"ok": False, "error": (r.stderr or r.stdout).strip()})
                return
            data = _json.loads(r.stdout)
        except FileNotFoundError:
            self._json({"ok": False, "error": "kubectl が見つかりません"})
            return
        except subprocess.TimeoutExpired:
            self._json({"ok": False, "error": "タイムアウト (15s)"})
            return
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)})
            return

        items = []
        for cj in data.get("items", []):
            meta   = cj.get("metadata", {})
            spec   = cj.get("spec", {})
            status = cj.get("status", {})

            last_sched   = status.get("lastScheduleTime")
            last_success = status.get("lastSuccessfulTime")
            active       = len(status.get("active") or [])
            suspended    = spec.get("suspend", False)

            # Determine status badge
            if suspended:
                badge = "suspended"
            elif active > 0:
                badge = "running"
            elif not last_sched:
                badge = "never"
            elif last_success:
                ts_sched   = datetime.fromisoformat(last_sched.replace("Z", "+00:00"))
                ts_success = datetime.fromisoformat(last_success.replace("Z", "+00:00"))
                badge = "ok" if ts_success >= ts_sched else "failed"
            else:
                badge = "failed"

            items.append({
                "name":        meta.get("name"),
                "namespace":   meta.get("namespace"),
                "schedule":    spec.get("schedule"),
                "active":      active,
                "suspended":   suspended,
                "lastScheduleTime":  last_sched,
                "lastSuccessfulTime": last_success,
                "badge":       badge,
            })

        self._json({"ok": True, "items": items})

    def _api_get_helo(self):
        try:
            content = CONFIGMAP_PATH.read_text(encoding="utf-8")
            self._json({"ok": True, "content": content})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)})

    def _api_post_helo(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self._json({"ok": False, "error": "リクエストの JSON が不正です"})
            return

        new_content = data.get("content", "")
        if "kind: ConfigMap" not in new_content:
            self._json({"ok": False, "error": "有効な ConfigMap YAML ではありません (kind: ConfigMap が見つかりません)"})
            return

        # Write the updated configmap
        try:
            CONFIGMAP_PATH.write_text(new_content, encoding="utf-8")
        except Exception as exc:
            self._json({"ok": False, "error": f"ファイル書き込みエラー: {exc}"})
            return

        # Run kubectl commands sequentially
        cmds = [
            ["kubectl", "apply", "-f", str(CONFIGMAP_PATH)],
            ["kubectl", "rollout", "restart", "deployment/postfix-deployment-a"],
            ["kubectl", "rollout", "restart", "deployment/postfix-deployment-b"],
        ]
        lines = []
        for cmd in cmds:
            lines.append("$ " + " ".join(cmd))
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if r.stdout.strip():
                    lines.append(r.stdout.strip())
                if r.stderr.strip():
                    lines.append(r.stderr.strip())
                if r.returncode != 0:
                    self._json({"ok": False, "error": "\n".join(lines)})
                    return
            except FileNotFoundError:
                self._json({"ok": False, "error": "kubectl が見つかりません (PATH を確認してください)"})
                return
            except subprocess.TimeoutExpired:
                self._json({"ok": False, "error": f"タイムアウト: {' '.join(cmd)}"})
                return
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
                return

        # recovery-helo-script (kube-system) を postfix-helo-script と同期する
        try:
            r = subprocess.run(
                ["kubectl", "get", "configmap", "postfix-helo-script", "-n", "default",
                 "-o", "jsonpath={.data.helo\\.sh}"],
                capture_output=True, text=True, timeout=30,
            )
            helo_sh = r.stdout
            if helo_sh:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                                delete=False, encoding="utf-8") as tmp:
                    tmp.write("apiVersion: v1\nkind: ConfigMap\n"
                              "metadata:\n  name: recovery-helo-script\n"
                              "  namespace: kube-system\n"
                              "data:\n  helo.sh: |\n")
                    for line in helo_sh.splitlines():
                        tmp.write(f"    {line}\n")
                    tmp_path = tmp.name
                r2 = subprocess.run(["kubectl", "apply", "-f", tmp_path],
                                    capture_output=True, text=True, timeout=30)
                os.unlink(tmp_path)
                lines.append("$ kubectl apply -f <tmpfile> (recovery-helo-script -n kube-system)")
                if r2.stdout.strip():
                    lines.append(r2.stdout.strip())
        except Exception:
            pass  # 同期失敗はメインの処理に影響させない

        self._json({"ok": True, "output": "\n".join(lines)})

    def _api_get_sc(self):
        try:
            content = RECIPIENT_CANONICAL_PATH.read_text(encoding="utf-8")
            pairs = _parse_sc_pairs(content)
            self._json({"ok": True, "pairs": pairs})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)})

    def _api_post_sc(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self._json({"ok": False, "error": "リクエストの JSON が不正です"})
            return

        pairs = data.get("pairs", [])
        new_content = _build_sc_yaml(pairs)

        try:
            RECIPIENT_CANONICAL_PATH.write_text(new_content, encoding="utf-8")
        except Exception as exc:
            self._json({"ok": False, "error": f"ファイル書き込みエラー: {exc}"})
            return

        cmds = [
            ["kubectl", "apply", "-f", str(RECIPIENT_CANONICAL_PATH)],
            ["kubectl", "rollout", "restart", "deployment/postfix-deployment-a"],
            ["kubectl", "rollout", "restart", "deployment/postfix-deployment-b"],
        ]
        lines = []
        for cmd in cmds:
            lines.append("$ " + " ".join(cmd))
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if r.stdout.strip():
                    lines.append(r.stdout.strip())
                if r.stderr.strip():
                    lines.append(r.stderr.strip())
                if r.returncode != 0:
                    self._json({"ok": False, "error": "\n".join(lines)})
                    return
            except FileNotFoundError:
                self._json({"ok": False, "error": "kubectl が見つかりません (PATH を確認してください)"})
                return
            except subprocess.TimeoutExpired:
                self._json({"ok": False, "error": f"タイムアウト: {' '.join(cmd)}"})
                return
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)})
                return

        # recovery-recipient-canonical (kube-system) を postfix-recipient-canonical と同期する
        try:
            r = subprocess.run(
                ["kubectl", "get", "configmap", "postfix-recipient-canonical", "-n", "default",
                 "-o", "jsonpath={.data.recipient_canonical}"],
                capture_output=True, text=True, timeout=30,
            )
            rc_data = r.stdout
            if rc_data:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                                delete=False, encoding="utf-8") as tmp:
                    tmp.write("apiVersion: v1\nkind: ConfigMap\n"
                              "metadata:\n  name: recovery-recipient-canonical\n"
                              "  namespace: kube-system\n"
                              "data:\n  recipient_canonical: |\n")
                    for line in rc_data.splitlines():
                        tmp.write(f"    {line}\n")
                    tmp_path = tmp.name
                r2 = subprocess.run(["kubectl", "apply", "-f", tmp_path],
                                    capture_output=True, text=True, timeout=30)
                os.unlink(tmp_path)
                lines.append("$ kubectl apply -f <tmpfile> (recovery-recipient-canonical -n kube-system)")
                if r2.stdout.strip():
                    lines.append(r2.stdout.strip())
        except Exception:
            pass  # 同期失敗はメインの処理に影響させない

        self._json({"ok": True, "output": "\n".join(lines)})

    def _api_get_metrics(self):
        def run_kubectl(args: list) -> tuple[bool, str]:
            try:
                r = subprocess.run(
                    ["kubectl"] + args,
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    return False, (r.stderr or r.stdout or "kubectl error").strip()
                return True, r.stdout
            except FileNotFoundError:
                return False, "kubectl が見つかりません"
            except subprocess.TimeoutExpired:
                return False, "タイムアウト (30s)"
            except Exception as exc:
                return False, str(exc)

        def parse_table(output: str) -> list:
            """Skip header line, split each data line by whitespace."""
            lines = output.strip().splitlines()
            if len(lines) < 2:
                return []
            return [line.split() for line in lines[1:] if line.strip()]

        ok1, out1 = run_kubectl(["top", "pods", "--all-namespaces", "--no-headers=false"])
        ok2, out2 = run_kubectl(["top", "nodes", "--no-headers=false"])
        ok3, out3 = run_kubectl(["get", "pods", "-o", "wide", "--all-namespaces", "--no-headers=false"])
        ok4, out4 = run_kubectl(["get", "nodes", "--no-headers=false"])
        ok5, out5 = run_kubectl(["get", "nodes", "-l", "role=gateway", "-o", "wide", "--no-headers=false"])
        ok6, out6 = run_kubectl([
            "get", "nodes", "-o",
            r'jsonpath={range .items[*]}{.metadata.name}{"\t"}{range .status.addresses[?(@.type=="ExternalIP")]}{.address}{end}{"\n"}{end}',
        ])
        ok7, out7 = run_kubectl([
            "get", "nodes", "-o",
            r'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.node\.kubernetes\.io/instance-type}{"\n"}{end}',
        ])

        if not ok1:
            self._json({"ok": False, "error": f"kubectl top pods: {out1}"})
            return
        if not ok2:
            self._json({"ok": False, "error": f"kubectl top nodes: {out2}"})
            return
        if not ok3:
            self._json({"ok": False, "error": f"kubectl get pods: {out3}"})
            return

        def parse_top_pods(output: str) -> list:
            """NAMESPACE NAME CPU MEMORY → drop NAMESPACE, return [NAME, CPU, MEMORY]."""
            lines = output.strip().splitlines()
            if len(lines) < 2:
                return []
            result = []
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 4:
                    result.append([parts[1], parts[2], parts[3]])
                elif len(parts) == 3:
                    result.append(parts)
            return result

        def parse_get_pods(output: str) -> list:
            """NAMESPACE NAME READY STATUS RESTARTS AGE IP NODE ... → drop NAMESPACE."""
            lines = output.strip().splitlines()
            if len(lines) < 2:
                return []
            result = []
            for line in lines[1:]:
                parts = line.split()
                # RESTARTS can be "3 (8d ago)" which adds 2 extra tokens, shifting AGE/IP/NODE
                offset = 2 if len(parts) > 5 and parts[5].startswith('(') else 0
                # NAMESPACE(0) NAME(1) READY(2) STATUS(3) RESTARTS(4) [ago tokens] AGE(5+off) IP(6+off) NODE(7+off)
                if len(parts) >= 8 + offset:
                    result.append([
                        parts[1],              # NAME
                        parts[2],              # READY
                        parts[3],              # STATUS
                        parts[4],              # RESTARTS
                        parts[5 + offset],     # AGE
                        parts[6 + offset],     # IP
                        parts[7 + offset],     # NODE
                    ])
            return result

        def parse_ext_ips_jsonpath(output: str) -> dict:
            """jsonpath NAME\tEXTERNALIP\n output → {name: ip} (only non-empty IPs)"""
            result = {}
            for line in output.strip().splitlines():
                parts = line.split('\t')
                if len(parts) == 2 and parts[1].strip():
                    result[parts[0].strip()] = parts[1].strip()
            return result

        def parse_gateway_nodes(output: str) -> tuple:
            """Return (names_list, ips_dict) for nodes with role=gateway label.
            NAME(0) STATUS(1) ROLES(2) AGE(3) VERSION(4) INTERNAL-IP(5) EXTERNAL-IP(6)
            """
            lines = output.strip().splitlines()
            if len(lines) < 2:
                return [], {}
            names = []
            ips = {}
            for line in lines[1:]:
                parts = line.split()
                if not parts:
                    continue
                name = parts[0]
                names.append(name)
                if len(parts) >= 7:
                    ext_ip = parts[6]
                    if ext_ip and ext_ip != "<none>":
                        ips[name] = ext_ip
            return names, ips

        def build_all_nodes(get_nodes_out: str, top_nodes_out: str) -> list:
            """All nodes from 'kubectl get nodes', enriched with metrics from 'kubectl top nodes'.
            Nodes missing from top nodes get placeholder values so all nodes are always shown."""
            metrics = {}
            for line in top_nodes_out.strip().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 5:
                    metrics[parts[0]] = [parts[1], parts[2], parts[3], parts[4]]
            result = []
            for line in get_nodes_out.strip().splitlines()[1:]:
                parts = line.split()
                if not parts:
                    continue
                name = parts[0]
                m = metrics.get(name, ['N/A', '0%', 'N/A', '0%'])
                result.append([name, m[0], m[1], m[2], m[3]])
            return result

        # ExternalIPs via jsonpath (works even when -o wide shows <none>)
        ext_ips = parse_ext_ips_jsonpath(out6) if ok6 else {}
        gw_names, gw_ips = parse_gateway_nodes(out5) if ok5 else ([], {})
        # jsonpath IPs are most reliable; label-query IPs fill gaps
        merged_ips = {**gw_ips, **ext_ips}

        # Instance types via node label
        node_instance_types = {}
        if ok7:
            for line in out7.strip().splitlines():
                parts = line.split('\t')
                if len(parts) == 2 and parts[1].strip():
                    node_instance_types[parts[0].strip()] = parts[1].strip()

        self._json({
            "ok": True,
            "top_pods":            parse_top_pods(out1),
            "top_nodes":           build_all_nodes(out4, out2) if ok4 else parse_table(out2),
            "get_pods":            parse_get_pods(out3),
            "node_ips":            merged_ips,
            "gateway_nodes":       gw_names,
            "node_instance_types": node_instance_types,
        })

    def _serve_static_html(self, filename: str):
        p = SCRIPT_DIR / filename
        if p.exists():
            self._send(200, "text/html; charset=utf-8", p.read_bytes())
        else:
            self._send(404, "text/plain", f"{filename} not found".encode())

    def _api_get_queue(self):
        now_jst = datetime.now(JST)
        with _queue_lock:
            snapshots = dict(_queue_snapshots)
        self._json({
            "ok": True,
            "timestamp": now_jst.isoformat(),
            "snapshots": snapshots,
        })

    def _api_get_s3_queue_count(self):
        self._json(_collect_s3_queue_counts())

    def _api_get_recovery_stats(self):
        self._json(_collect_recovery_stats())

    def _api_post_queue_snapshot(self):
        """手動スナップショット取得: キューを今すぐ収集して manual キーで保存する。"""
        global _queue_snapshots
        try:
            now_jst = datetime.now(JST)
            counts = _collect_queue_counts()
            snapshot = {
                "timestamp": now_jst.isoformat(),
                "pods": counts,
            }
            with _queue_lock:
                _queue_snapshots["manual"] = snapshot
                _save_queue_snapshots()
            self._json({"ok": True, "snapshot": snapshot})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)})

    def _api_post_node_snapshot_fetch(self):
        """手動でノード構成を今すぐ収集して manual キーで保存する。"""
        global _node_snapshots
        try:
            now_jst = datetime.now(JST)
            nodes = _collect_node_data()
            snapshot = {"timestamp": now_jst.isoformat(), "nodes": nodes}
            with _node_lock:
                _node_snapshots["manual"] = snapshot
                _save_node_snapshots()
            self._json({"ok": True, "snapshot": snapshot})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)})

    def _api_get_node_snapshots(self):
        with _node_lock:
            snapshots = dict(_node_snapshots)
        self._json({"ok": True, "snapshots": snapshots})

    def _api_post_node_snapshot(self):
        """CronJob から呼ばれるノード構成スナップショット受信エンドポイント。
        payload: {"label": "23:00", "timestamp": "...", "nodes": [...]}
        queue-snapshots.json と同様の形式で node-snapshots.json に保存する。
        """
        global _node_snapshots
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            self._json({"ok": False, "error": "リクエストの JSON が不正です"})
            return

        label = data.get("label", "").strip()
        if not label:
            self._json({"ok": False, "error": "label フィールドが必要です"})
            return

        snapshot = {
            "timestamp": data.get("timestamp", datetime.now(JST).isoformat()),
            "nodes": data.get("nodes", []),
        }
        with _node_lock:
            _node_snapshots[label] = snapshot
            _save_node_snapshots()
        self._json({"ok": True, "snapshot": snapshot})

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(200, "application/json; charset=utf-8", body)

    def log_message(self, fmt, *args):
        # Custom log format: timestamp  method  path  status
        msg = fmt % args
        print(f"  {self.log_date_time_string()}  {msg}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    PORT = 8080
    server = ThreadingHTTPServer(("0.0.0.0", PORT), AdminHandler)

    print("━" * 44, flush=True)
    print("  Mail Platform Admin Server", flush=True)
    print(f"  URL      :  http://0.0.0.0:{PORT}", flush=True)
    print(f"  Username :  {USERNAME}", flush=True)
    print(f"  Password :  {PASSWORD}", flush=True)
    print("━" * 44, flush=True)
    print("  Ctrl+C で停止", flush=True)
    print(flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました")
        server.server_close()
