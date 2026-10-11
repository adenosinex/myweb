# blueprints/mem.py
"""
/mem 蓝图：Mac mini 内存概览（大改版）

设计变更:
    · 删除页面级内存细节 (active / inactive / wired / compressor / purgeable ...)
    · 内存只保留"大致占用"——总量 / 已用 / 可用 / 使用率 / 压力等级 / Swap
    · 新增 TOP 内存进程表格
    · 新增 24h 内存趋势折线图（纯 Canvas，无第三方库）
    · 新增优化建议规则引擎（异常优先显示）

路由:
    GET /mem            → HTML 页面
    GET /mem/api        → JSON 实时数据
    GET /mem/history    → JSON 历史采样（供折线图）

SQLite:
    db/metrics.db → mem_samples 表，30s 采样，保留 7 天
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template_string, request

mem_bp = Blueprint("mem", __name__)

# ============================================================ 常量

MEM_PRESSURE_CMD = shutil.which("memory_pressure") or "/usr/bin/memory_pressure"
SYSCTL_CMD = shutil.which("sysctl") or "/usr/sbin/sysctl"
PS_CMD = shutil.which("ps") or "/bin/ps"

CACHE_TTL = 3.0
SAMPLE_INTERVAL = 30
RETENTION_DAYS = 7

DB_DIR = os.path.join(os.getcwd(), "db")
DB_PATH = os.path.join(DB_DIR, "metrics.db")

_PRESSURE_LEVELS = {"1": "normal", "2": "warn", "4": "critical"}
_UNIT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


# ============================================================ 底层命令

def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def _to_bytes(text: str):
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?)", text or "")
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _UNIT.get(m.group(2).upper(), 1))
    except ValueError:
        return None


# ============================================================ 内存采集（只取大数）

def _read_total_memory():
    res = _run([SYSCTL_CMD, "-n", "hw.memsize"])
    if res and res.returncode == 0:
        try:
            return int(res.stdout.strip())
        except ValueError:
            pass
    return None


def _read_free_percentage():
    """memory_pressure 输出的 'System-wide memory free percentage: N%'。"""
    res = _run([MEM_PRESSURE_CMD])
    if not res or res.returncode != 0:
        return None
    m = re.search(r"System-wide memory free percentage:\s+(\d+)%", res.stdout)
    return int(m.group(1)) if m else None


def _read_pressure_level():
    res = _run([SYSCTL_CMD, "-n", "kern.memorystatus_vm_pressure_level"])
    if not res or res.returncode != 0:
        return "unknown"
    return _PRESSURE_LEVELS.get(res.stdout.strip(), "unknown")


def _read_swap():
    res = _run([SYSCTL_CMD, "-n", "vm.swapusage"])
    if not res or res.returncode != 0:
        return {}
    out = res.stdout.strip()
    swap = {}
    for key in ("total", "used", "free"):
        m = re.search(rf"{key}\s*=\s*([\d.]+\s*[KMGT]?)", out, re.IGNORECASE)
        if m:
            b = _to_bytes(m.group(1))
            if b is not None:
                swap[key + "_bytes"] = b
    total, used = swap.get("total_bytes"), swap.get("used_bytes")
    if total:
        swap["used_percentage"] = round((used or 0) / total * 100, 1)
    swap["encrypted"] = "encrypted" in out.lower()
    return swap


def _read_top_mem_processes(limit: int = 8) -> list:
    """ps -m 按内存排序，取前 N 个进程。"""
    res = _run([PS_CMD, "-axo", "pid,comm,%cpu,rss", "-m"])
    if not res or res.returncode != 0:
        return []

    lines = res.stdout.strip().splitlines()
    procs = []
    for line in lines[1: limit + 1]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            name = os.path.basename(parts[1])
            cpu = float(parts[2])
            rss_kb = int(parts[3])
        except ValueError:
            continue
        procs.append({
            "pid": pid,
            "name": name,
            "cpu": round(cpu, 1),
            "rss_bytes": rss_kb * 1024,
        })
    return procs


# ============================================================ 组装 + 建议

def _build_payload() -> dict:
    total = _read_total_memory()
    free_pct = _read_free_percentage()
    swap = _read_swap()
    level = _read_pressure_level()

    if total and free_pct is not None:
        free_bytes = int(total * free_pct / 100)
        used_bytes = total - free_bytes
        used_pct = round(100 - free_pct, 1)
    else:
        free_bytes = used_bytes = used_pct = None

    top = _read_top_mem_processes(8)
    # 计算每个进程占总量百分比
    if total:
        for p in top:
            p["mem_pct"] = round(p["rss_bytes"] / total * 100, 1)
    else:
        for p in top:
            p["mem_pct"] = None

    payload = {
        "timestamp": int(time.time()),
        "total_bytes": total,
        "used_bytes": used_bytes,
        "free_bytes": free_bytes,
        "used_percentage": used_pct,
        "free_percentage": free_pct,
        "pressure_level": level,
        "swap": swap,
        "top_processes": top,
    }
    payload["suggestions"] = _make_suggestions(payload)
    return payload


def _make_suggestions(p: dict) -> list:
    tips = []

    swap_used = (p.get("swap") or {}).get("used_bytes") or 0
    if swap_used > 0:
        mb = swap_used / 1024 / 1024
        tips.append({"level": "warn",
                     "text": f"存在交换（{mb:.0f} MB），建议检查内存占用进程"})

    fp = p.get("free_percentage")
    if fp is not None and fp < 20:
        tips.append({"level": "danger" if fp < 10 else "warn",
                     "text": f"系统空闲内存仅 {fp}%，考虑关闭不必要进程"})

    level = p.get("pressure_level")
    if level == "critical":
        tips.insert(0, {"level": "danger", "text": "系统内存压力等级：CRITICAL，请立即处理"})
    elif level == "warn":
        tips.append({"level": "warn", "text": "系统内存压力等级：WARN，注意观察"})

    # 单进程占用 > 25% 提示
    for proc in (p.get("top_processes") or []):
        if proc.get("mem_pct") and proc["mem_pct"] >= 25:
            tips.append({"level": "warn",
                         "text": f"进程 {proc['name']} (PID {proc['pid']}) 占用 {proc['mem_pct']}% 内存"})
            break

    if not tips:
        tips.append({"level": "ok", "text": "内存状态正常，无异常需要处理"})
    return tips


# ============================================================ 缓存

_cache = {"ts": 0.0, "data": None}
_cache_lock = threading.Lock()


def get_memory_overview() -> dict:
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
            return _cache["data"]

    data = _build_payload()

    with _cache_lock:
        _cache["ts"] = now
        _cache["data"] = data
    return data


# ============================================================ SQLite 历史

def _ensure_db():
    os.makedirs(DB_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mem_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            total_bytes INTEGER,
            used_bytes INTEGER,
            free_pct INTEGER,
            swap_bytes INTEGER,
            pressure TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts ON mem_samples(timestamp)")
    con.commit()
    return con


def _record_sample():
    while True:
        try:
            d = get_memory_overview()
            if "error" not in d:
                con = _ensure_db()
                con.execute(
                    "INSERT INTO mem_samples "
                    "(timestamp, total_bytes, used_bytes, free_pct, swap_bytes, pressure) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        d.get("timestamp") or int(time.time()),
                        d.get("total_bytes"),
                        d.get("used_bytes"),
                        d.get("free_percentage"),
                        (d.get("swap") or {}).get("used_bytes"),
                        d.get("pressure_level"),
                    ),
                )
                cutoff = int((datetime.now() - timedelta(days=RETENTION_DAYS)).timestamp())
                con.execute("DELETE FROM mem_samples WHERE timestamp < ?", (cutoff,))
                con.commit()
                con.close()
        except Exception:
            pass
        time.sleep(SAMPLE_INTERVAL)


def start_sampler():
    """启动后台采样线程（app 启动时调用一次）。"""
    t = threading.Thread(target=_record_sample, daemon=True, name="mem-sampler")
    t.start()


# ============================================================ 路由

@mem_bp.get("/mem/api")
def mem_api():
    resp = jsonify(get_memory_overview())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@mem_bp.get("/mem/history")
def mem_history():
    hours = min(int(request.args.get("hours", 24)), RETENTION_DAYS * 24)
    since = int((datetime.now() - timedelta(hours=hours)).timestamp())
    try:
        con = _ensure_db()
        rows = con.execute(
            "SELECT timestamp, used_bytes, free_pct, swap_bytes, pressure "
            "FROM mem_samples WHERE timestamp >= ? ORDER BY timestamp ASC",
            (since,),
        ).fetchall()
        con.close()
    except Exception as e:
        return jsonify({"error": str(e), "samples": []})

    return jsonify({
        "hours": hours,
        "samples": [
            {"ts": r[0], "used_bytes": r[1], "free_pct": r[2],
             "swap_bytes": r[3], "pressure": r[4]}
            for r in rows
        ],
    })


@mem_bp.get("/mem")
def mem_page():
    return render_template_string(PAGE_HTML)


# ============================================================ 前端页面

PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Memory · Mac mini Monitor</title>
<style>
  :root {
    --bg: #0e1016;
    --panel: #171a21;
    --panel-2: #1e222b;
    --line: #2a2f3a;
    --text: #e8eaf0;
    --muted: #8a92a6;
    --green: #34d399;
    --yellow: #fbbf24;
    --red: #f87171;
    --blue: #60a5fa;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue",
                 Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: radial-gradient(1000px 500px at 50% -20%, #1c2333 0%, var(--bg) 55%);
    color: var(--text);
    min-height: 100vh;
    padding: 24px 16px 60px;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1200px; margin: 0 auto; }

  /* Header */
  header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 18px; flex-wrap: wrap; gap: 12px;
  }
  header h1 {
    font-size: 20px; margin: 0; font-weight: 600; letter-spacing: .3px;
    display: flex; align-items: center; gap: 10px;
  }
  header h1 .dot {
    width: 9px; height: 9px; border-radius: 50%; background: var(--green);
    box-shadow: 0 0 12px var(--green); transition: .3s;
  }
  header .meta {
    color: var(--muted); font-size: 13px;
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  }
  .refresh-btn {
    background: var(--panel-2); border: 1px solid var(--line); color: var(--text);
    padding: 6px 12px; border-radius: 8px; font-size: 13px; cursor: pointer;
    transition: .15s;
  }
  .refresh-btn:hover { background: #262c38; border-color: #3a4152; }

  /* Tips */
  #tips { margin-bottom: 14px; display: flex; flex-direction: column; gap: 8px; }
  .tip {
    display: flex; align-items: center; gap: 10px;
    padding: 11px 15px; border-radius: 10px; font-size: 14px;
    border: 1px solid var(--line); background: var(--panel);
  }
  .tip .ico { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .tip.ok     { border-color: rgba(52,211,153,.35);  background: rgba(52,211,153,.06); }
  .tip.ok     .ico { background: var(--green); }
  .tip.warn   { border-color: rgba(251,191,36,.40);  background: rgba(251,191,36,.07); color: #fde68a; }
  .tip.warn   .ico { background: var(--yellow); }
  .tip.danger { border-color: rgba(248,113,113,.50); background: rgba(248,113,113,.09); color: #fecaca; }
  .tip.danger .ico { background: var(--red); }

  /* Hero */
  .hero {
    background: linear-gradient(180deg, var(--panel) 0%, #14171e 100%);
    border: 1px solid var(--line); border-radius: 18px;
    padding: 26px 26px 22px; margin-bottom: 14px;
    position: relative; overflow: hidden;
  }
  .hero::before {
    content: ""; position: absolute; inset: 0;
    background: radial-gradient(400px 160px at 20% 0%, rgba(52,211,153,.14), transparent 70%);
    pointer-events: none; opacity: .7;
  }
  .hero-label { color: var(--muted); font-size: 13px; letter-spacing: .5px; }
  .hero-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-top: 4px; }
  .big-num { font-size: 64px; font-weight: 700; line-height: 1; letter-spacing: -2px; font-variant-numeric: tabular-nums; }
  .big-num .pct { font-size: 26px; opacity: .65; margin-left: 4px; font-weight: 600; }
  .hero-sub {
    color: var(--muted); font-size: 13px; margin-top: 14px;
    display: flex; gap: 20px; flex-wrap: wrap;
  }
  .hero-sub b { color: var(--text); font-weight: 600; }

  .badge {
    font-size: 11px; font-weight: 700; letter-spacing: 1.2px;
    padding: 5px 11px; border-radius: 999px;
    border: 1px solid var(--line); background: var(--panel-2);
    color: var(--muted); white-space: nowrap;
  }
  .badge.normal   { color: var(--green);  border-color: rgba(52,211,153,.45);  background: rgba(52,211,153,.10); }
  .badge.warn     { color: var(--yellow); border-color: rgba(251,191,36,.45);  background: rgba(251,191,36,.10); }
  .badge.critical { color: var(--red);    border-color: rgba(248,113,113,.45); background: rgba(248,113,113,.10); }

  .bar {
    height: 10px; background: #0b0d12; border-radius: 999px; overflow: hidden;
    margin-top: 16px; border: 1px solid var(--line);
  }
  .bar > span {
    display: block; height: 100%; border-radius: 999px;
    transition: width .5s ease, background .3s;
  }

  /* Grid */
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 14px; margin-bottom: 14px;
  }
  .grid.wide { grid-template-columns: 1fr; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 14px; padding: 16px 18px;
  }
  .card h3 {
    margin: 0 0 12px; font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 1.2px; color: var(--muted);
    display: flex; justify-content: space-between; align-items: center;
  }
  .kv {
    display: flex; justify-content: space-between; padding: 7px 0;
    border-bottom: 1px dashed #232833; font-size: 14px;
  }
  .kv:last-child { border-bottom: none; }
  .kv .k { color: var(--muted); }
  .kv .v { font-variant-numeric: tabular-nums; font-weight: 500; }

  /* 表格 */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td {
    text-align: left; padding: 8px 6px;
    border-bottom: 1px dashed #232833;
    font-variant-numeric: tabular-nums;
  }
  th {
    color: var(--muted); font-weight: 500; font-size: 11px;
    text-transform: uppercase; letter-spacing: .8px;
  }
  tr:last-child td { border-bottom: none; }
  td.num, th.num { text-align: right; }
  .name { color: var(--blue); font-weight: 500; }
  .cpu-hi  { color: var(--yellow); }
  .cpu-vhi { color: var(--red); }

  /* 图表 */
  .chart-box { position: relative; width: 100%; height: 200px; }
  canvas { width: 100%; height: 100%; display: block; }
  .legend {
    display: flex; gap: 16px; font-size: 12px; color: var(--muted);
    margin-top: 8px; flex-wrap: wrap;
  }
  .legend i { display: inline-block; width: 10px; height: 3px; border-radius: 2px; vertical-align: middle; margin-right: 5px; }

  .err {
    background: rgba(248,113,113,.1); border: 1px solid rgba(248,113,113,.4);
    color: #fecaca; padding: 14px 16px; border-radius: 12px; font-size: 14px;
    margin-bottom: 16px; display: none;
  }
  footer { text-align: center; color: var(--muted); font-size: 12px; margin-top: 26px; }

  @media (max-width: 480px) {
    .big-num { font-size: 46px; }
    .hero { padding: 20px; }
    .card { padding: 14px; }
    .chart-box { height: 160px; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span class="dot" id="dot"></span> Memory · Mac mini Monitor</h1>
    <div class="meta">
      <span id="lastUpdate">--</span>
      <span id="nextTick">--</span>
      <button class="refresh-btn" onclick="load()">刷新</button>
    </div>
  </header>

  <div id="errBox" class="err"></div>
  <div id="tips"></div>

  <!-- Hero：内存占用大数 -->
  <section class="hero">
    <div class="hero-label">内存已用</div>
    <div class="hero-row">
      <div class="big-num">
        <span id="usedVal">--</span><span class="pct" id="pctSign">%</span>
      </div>
      <span class="badge" id="pressureBadge">UNKNOWN</span>
    </div>
    <div class="bar"><span id="barFill" style="width:0%"></span></div>
    <div class="hero-sub">
      <span>总量 <b id="total">--</b></span>
      <span>已用 <b id="usedBytes">--</b></span>
      <span>可用 <b id="freeBytes">--</b></span>
    </div>
  </section>

  <!-- 摘要卡片 -->
  <div class="grid">
    <div class="card">
      <h3>Swap 概览</h3>
      <div class="kv"><span class="k">总量</span><span class="v" id="swapTotal">--</span></div>
      <div class="kv"><span class="k">已用</span><span class="v" id="swapUsed">--</span></div>
      <div class="kv"><span class="k">剩余</span><span class="v" id="swapFree">--</span></div>
      <div class="kv"><span class="k">加密</span><span class="v" id="swapEnc">--</span></div>
    </div>

    <div class="card">
      <h3>内存使用率</h3>
      <div class="kv"><span class="k">已用百分比</span><span class="v" id="usedPct">--</span></div>
      <div class="kv"><span class="k">可用百分比</span><span class="v" id="freePct">--</span></div>
      <div class="kv"><span class="k">压力等级</span><span class="v" id="pressureTxt">--</span></div>
    </div>
  </div>

  <!-- TOP 进程 -->
  <div class="grid wide">
    <div class="card">
      <h3>内存占用 TOP 进程 <span style="color:var(--muted);text-transform:none;letter-spacing:0;font-weight:400;">按 RSS 排序</span></h3>
      <table>
        <thead>
          <tr>
            <th>进程</th>
            <th class="num">PID</th>
            <th class="num">内存</th>
            <th class="num">占比</th>
            <th class="num">CPU</th>
          </tr>
        </thead>
        <tbody id="procBody">
          <tr><td colspan="5" style="color:var(--muted)">加载中…</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- 24h 趋势 -->
  <div class="grid wide">
    <div class="card">
      <h3>最近 24 小时趋势</h3>
      <div class="chart-box"><canvas id="chart"></canvas></div>
      <div class="legend">
        <span><i style="background:var(--blue)"></i>已用内存</span>
        <span><i style="background:var(--yellow)"></i>Swap</span>
      </div>
    </div>
  </div>

  <footer>数据来源：sysctl · memory_pressure · ps · 5 秒自动刷新 · 30 秒采样 / 保留 7 天</footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const REFRESH_MS = 5000;
const HISTORY_MS = 60000;
let lastLoad = 0;
let inflight = false;

const LEVEL_TEXT = { normal: "NORMAL", warn: "WARN", critical: "CRITICAL" };
let HISTORY = [];

function fmtBytes(b) {
  if (b == null) return "--";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = b;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 ? 0 : v >= 10 ? 1 : 2) + " " + units[i];
}
function fmtNum(n) { return n == null ? "--" : n.toLocaleString(); }

/* 已用内存越多越糟 */
function colorForUsed(pct) {
  if (pct == null) return "var(--muted)";
  if (pct >= 85) return "var(--red)";
  if (pct >= 65) return "var(--yellow)";
  return "var(--green)";
}

function renderTips(list) {
  const box = $("tips");
  box.innerHTML = "";
  (list || []).forEach(t => {
    const div = document.createElement("div");
    div.className = "tip " + (t.level || "ok");
    div.innerHTML = `<span class="ico"></span><span>${t.text}</span>`;
    box.appendChild(div);
  });
}

function renderProcs(list) {
  const body = $("procBody");
  body.innerHTML = "";
  if (!list || !list.length) {
    body.innerHTML = `<tr><td colspan="5" style="color:var(--muted)">暂无数据</td></tr>`;
    return;
  }
  list.forEach(p => {
    const tr = document.createElement("tr");
    const cpuCls = p.cpu >= 80 ? "cpu-vhi" : p.cpu >= 40 ? "cpu-hi" : "";
    tr.innerHTML = `
      <td class="name">${p.name}</td>
      <td class="num">${p.pid}</td>
      <td class="num">${fmtBytes(p.rss_bytes)}</td>
      <td class="num">${p.mem_pct == null ? "--" : p.mem_pct + "%"}</td>
      <td class="num ${cpuCls}">${p.cpu}%</td>
    `;
    body.appendChild(tr);
  });
}

function render(d) {
  if (!d || d.error) {
    $("errBox").style.display = "block";
    $("errBox").textContent = "获取失败：" + ((d && d.error) || "未知错误");
    return;
  }
  $("errBox").style.display = "none";
  renderTips(d.suggestions);
  renderProcs(d.top_processes);

  const used = d.used_percentage;
  const color = colorForUsed(used);
  $("usedVal").textContent = used == null ? "--" : used;
  $("usedVal").style.color = color;
  $("pctSign").style.display = used == null ? "none" : "inline";
  $("barFill").style.width = (used == null ? 0 : Math.min(100, used)) + "%";
  $("barFill").style.background = color;
  $("dot").style.background = color;
  $("dot").style.boxShadow = `0 0 12px ${color}`;

  const lvl = d.pressure_level || "unknown";
  const badge = $("pressureBadge");
  badge.textContent = LEVEL_TEXT[lvl] || "UNKNOWN";
  badge.className = "badge " + lvl;

  $("total").textContent = fmtBytes(d.total_bytes);
  $("usedBytes").textContent = fmtBytes(d.used_bytes);
  $("freeBytes").textContent = fmtBytes(d.free_bytes);

  const S = d.swap || {};
  $("swapTotal").textContent = fmtBytes(S.total_bytes);
  $("swapUsed").textContent = fmtBytes(S.used_bytes) +
    (S.used_percentage != null ? ` (${S.used_percentage}%)` : "");
  $("swapFree").textContent = fmtBytes(S.free_bytes);
  $("swapEnc").textContent = S.encrypted ? "已加密" : "—";
  $("swapUsed").style.color =
    (S.used_percentage != null && S.used_percentage > 50) ? "var(--yellow)" : "var(--text)";

  $("usedPct").textContent = used == null ? "--" : used + "%";
  $("freePct").textContent = d.free_percentage == null ? "--" : d.free_percentage + "%";
  $("pressureTxt").textContent = (LEVEL_TEXT[lvl] || "UNKNOWN");

  $("lastUpdate").textContent = "更新于 " + new Date().toLocaleTimeString();
}

/* ---------------- 折线图 ---------------- */
function drawChart() {
  const cvs = $("chart");
  const rect = cvs.getBoundingClientRect();
  if (rect.width === 0) return;
  const dpr = window.devicePixelRatio || 1;
  cvs.width = rect.width * dpr;
  cvs.height = rect.height * dpr;
  const ctx = cvs.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);

  const W = rect.width, H = rect.height;
  const PAD = { l: 46, r: 10, t: 12, b: 22 };
  const innerW = W - PAD.l - PAD.r;
  const innerH = H - PAD.t - PAD.b;

  // 网格
  ctx.strokeStyle = "#232833";
  ctx.lineWidth = 1;
  ctx.font = "11px -apple-system, sans-serif";
  ctx.fillStyle = "#8a92a6";
  for (let i = 0; i <= 4; i++) {
    const y = PAD.t + innerH * i / 4;
    ctx.beginPath();
    ctx.moveTo(PAD.l, y);
    ctx.lineTo(PAD.l + innerW, y);
    ctx.stroke();
    ctx.fillText((100 - i * 25) + "%", 4, y + 4);
  }

  if (!HISTORY.length) {
    ctx.fillStyle = "#8a92a6";
    ctx.fillText("暂无历史数据", PAD.l + 8, PAD.t + innerH / 2);
    return;
  }

  const now = Date.now() / 1000;
  const t0 = now - 24 * 3600;
  const total = HISTORY[0].__total || null;

  function xOf(ts) { return PAD.l + (ts - t0) / (now - t0) * innerW; }
  function yOf(pct) { return PAD.t + innerH * (1 - pct / 100); }

  // 已用内存百分比
  ctx.strokeStyle = "#60a5fa";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  let started = false;
  HISTORY.forEach(s => {
    if (s.used_pct == null) return;
    const x = xOf(s.ts), y = yOf(s.used_pct);
    if (!started) { ctx.moveTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Swap 百分比（相对物理内存的比例）
  const swapPts = HISTORY.filter(s => s.swap_bytes != null && total);
  if (swapPts.length) {
    ctx.strokeStyle = "#fbbf24";
    ctx.beginPath();
    let st2 = false;
    swapPts.forEach(s => {
      const pct = Math.min(100, s.swap_bytes / total * 100);
      const x = xOf(s.ts), y = yOf(pct);
      if (!st2) { ctx.moveTo(x, y); st2 = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  // X 轴时间标注
  ctx.fillStyle = "#8a92a6";
  for (let i = 0; i <= 4; i++) {
    const ts = t0 + (now - t0) * i / 4;
    const x = PAD.l + innerW * i / 4;
    const d = new Date(ts * 1000);
    const txt = d.getHours().toString().padStart(2, "0") + ":00";
    const tw = ctx.measureText(txt).width;
    ctx.fillText(txt, Math.min(W - PAD.r - tw, Math.max(PAD.l, x - tw / 2)), H - 6);
  }
}

async function loadHistory() {
  try {
    const r = await fetch("/mem/history?hours=24", { cache: "no-store" });
    const d = await r.json();
    const samples = d.samples || [];
    const total = samples.length ? Math.max(...samples.map(s => s.used_bytes || 0)) : 0;
    HISTORY = samples.map(s => ({
      ts: s.ts,
      used_pct: (s.free_pct != null) ? 100 - s.free_pct : null,
      swap_bytes: s.swap_bytes,
      __total: s.used_bytes && s.free_pct != null
        ? s.used_bytes / (100 - s.free_pct) * 100
        : null,
    }));
    drawChart();
  } catch (e) {
    /* ignore */
  }
}

async function load() {
  if (inflight) return;
  inflight = true;
  lastLoad = Date.now();
  try {
    const r = await fetch("/mem/api", { cache: "no-store" });
    render(await r.json());
  } catch (e) {
    $("errBox").style.display = "block";
    $("errBox").textContent = "请求失败：" + e;
  } finally {
    inflight = false;
  }
}

setInterval(() => {
  const elapsed = Date.now() - lastLoad;
  const remain = Math.max(0, Math.ceil((REFRESH_MS - elapsed) / 1000));
  $("nextTick").textContent = remain > 0 ? `${remain}s 后刷新` : "刷新中…";
  if (elapsed >= REFRESH_MS) load();
}, 500);

setInterval(loadHistory, HISTORY_MS);
window.addEventListener("resize", drawChart);

load();
loadHistory();
</script>
</body>
</html>
"""