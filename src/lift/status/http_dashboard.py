"""HTTP 状态仪表盘：浏览器侧实时观察 LIFT 评测进度（零额外依赖，标准库实现）。

设计目标：在 ``--status-viz`` 终端 TUI 之外，再提供一个浏览器端的实时仪表盘，
解决以下场景：

- ``nohup`` / 远端机器跑评测时没有 tty 也想看实时状态
- 多人同时观察一次 run（团队会议 / 协同调试）

实现方式：

- 复用 ``src.lift.status.events`` 事件总线，注册一个监听器把事件追加到环形缓冲；
  同时持有一个 ``RunStateTracker``，用于响应 ``GET /snapshot`` 全量请求。
- 用标准库 ``http.server.ThreadingHTTPServer`` 起后台线程，零外部依赖。
- 路由：

  - ``GET /``：返回内嵌单文件 HTML（前端展示与 TUI 同等信息：Header/Repeats/
    Suites×Repeats 栅格/Containers）。
  - ``GET /snapshot``：返回 ``RunSnapshot`` 的 JSON，前端进入页面时一次性拉取。
  - ``GET /events``：Server-Sent Events 长连接，连接建立后先推一份 snapshot，
    然后随事件总线实时推送增量；客户端用 ``EventSource`` 接收并合并到本地状态。

线程安全：``RunStateTracker.snapshot()`` 已经在锁下深拷贝；事件 fan-out 由
``events`` 模块保证。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import asdict, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from src.lift.status import events as ev
from src.lift.status.state import RunSnapshot, RunStateTracker

LOGGER = logging.getLogger(__name__)

_SSE_KEEPALIVE_SECONDS = 15.0  # 没有事件时定期发心跳，防止反向代理断连


# ---- 事件 / 快照 → JSON 的纯函数 ----------------------------------------


def _to_jsonable(obj: Any) -> Any:
    """递归把 dataclass / dict / list 转成 JSON 可序列化结构。"""
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    return obj


def _snapshot_payload(snapshot: RunSnapshot) -> dict[str, Any]:
    """把 ``RunSnapshot`` 序列化成前端易消费的 JSON 结构。"""
    return _to_jsonable(snapshot)


def _event_payload(event: object) -> dict[str, Any]:
    """把事件 dataclass 序列化为 ``{type, data}``。"""
    return {"type": type(event).__name__, "data": _to_jsonable(event)}


# ---- HTML 前端（单文件内嵌） ---------------------------------------------


_INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>LIFT status</title>
<style>
  :root {
    --fg: #e5e7eb; --bg: #0b1020; --muted: #94a3b8; --line: #1f2937;
    --green: #10b981; --yellow: #f59e0b; --red: #ef4444; --blue: #3b82f6;
    --grey: #475569;
  }
  body { background: var(--bg); color: var(--fg); font: 13px/1.45 ui-monospace, Menlo, Consolas, monospace; margin: 0; padding: 16px; }
  h1 { font-size: 14px; font-weight: 600; margin: 0 0 8px; color: var(--blue); }
  .panel { border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px; margin-bottom: 12px; }
  .panel-title { color: var(--blue); margin-bottom: 6px; font-weight: 600; }
  .row { display: flex; align-items: center; gap: 8px; }
  .bar { background: var(--line); height: 8px; border-radius: 4px; overflow: hidden; flex: 1; min-width: 80px; }
  .bar > div { background: var(--blue); height: 100%; transition: width .3s ease; }
  .muted { color: var(--muted); }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; }
  td.cell { font-family: ui-monospace; letter-spacing: 1px; white-space: nowrap; }
  .pending { color: var(--grey); }
  .running { color: var(--yellow); }
  .retrying { color: var(--yellow); }
  .done { color: var(--green); }
  .failed { color: var(--red); }
  .legend { color: var(--muted); font-size: 12px; margin-top: 6px; }
  .controls { float: right; }
  input[type=text] { background: #0f172a; color: var(--fg); border: 1px solid var(--line); border-radius: 4px; padding: 2px 6px; }
  .conn { font-size: 11px; padding: 2px 6px; border-radius: 4px; }
  .conn.live { background: #064e3b; color: #6ee7b7; }
  .conn.dead { background: #7f1d1d; color: #fecaca; }
  .params { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px 16px; }
  .params .pk { color: var(--muted); margin-right: 6px; }
  .params .pv { color: var(--fg); word-break: break-all; }
  /* 自定义 tooltip：hover 即弹（绕开浏览器原生 title 的 ~1s 延迟） */
  .tip { position: relative; }
  .tip:hover::after {
    content: attr(data-tip);
    position: absolute;
    left: 0; top: 100%;
    margin-top: 4px;
    z-index: 10;
    background: #0f172a;
    color: var(--fg);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 6px 8px;
    white-space: pre;
    font: 12px/1.4 ui-monospace, Menlo, Consolas, monospace;
    box-shadow: 0 4px 12px rgba(0,0,0,.4);
    pointer-events: none;
    max-width: 480px;
  }
</style>
</head>
<body>
<h1 id="title">LIFT status <span id="conn" class="conn dead">disconnected</span></h1>

<div class="panel" id="header-panel">
  <div class="panel-title">run</div>
  <div class="row">
    <div id="run-id" class="muted"></div>
    <div class="bar"><div id="overall-bar" style="width:0"></div></div>
    <div id="overall-stats" class="muted"></div>
  </div>
</div>

<div class="panel" id="params-panel" style="display:none">
  <div class="panel-title">run params</div>
  <div id="params" class="params"></div>
</div>

<div class="panel" id="repeats-panel">
  <div class="panel-title">repeats</div>
  <div id="repeats"></div>
</div>

<div class="panel">
  <div class="panel-title">
    suites × repeats
    <span class="controls">
      <label class="muted"><input type="checkbox" id="hide-done" /> hide done</label>
      <input type="text" id="filter" placeholder="filter suite..." />
    </span>
  </div>
  <table id="grid"><thead></thead><tbody></tbody></table>
  <div class="legend">legend: w=warmup b=baseline e=evolved · · pending ◔ running ↻ retrying ● done ✗ failed · hover for details</div>
</div>

<div class="panel">
  <div class="panel-title">alive containers <span id="ctr-count" class="muted"></span></div>
  <table id="ctr"><thead><tr><th>container</th><th>repeat</th><th>stage</th><th>suite</th><th>task</th><th>uptime</th></tr></thead><tbody></tbody></table>
</div>

<div class="panel" id="errors-panel" style="display:none">
  <div class="panel-title">recent failures <span id="err-count" class="muted"></span></div>
  <table id="errors"><thead><tr><th>time</th><th>kind</th><th>where</th><th>detail</th></tr></thead><tbody></tbody></table>
</div>

<script>
const STATUS_SYM = { pending: '·', running: '◔', retrying: '↻', done: '●', failed: '✗' };
let snapshot = null;

function escapeAttr(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function fmtDuration(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h) return `${h}h${m.toString().padStart(2, '0')}m`;
  if (m) return `${m}m${s.toString().padStart(2, '0')}s`;
  return `${s}s`;
}

function suiteOverall(suite) {
  // 与 TUI 取最差状态：failed > running/retrying > pending > done
  const all = [];
  if (suite.warmup_status) all.push(suite.warmup_status);
  for (const t of Object.values(suite.holdout_tasks || {})) {
    for (const p of Object.values(t.phases || {})) all.push(p.status);
  }
  if (all.includes('failed')) return 'failed';
  if (all.includes('running') || all.includes('retrying')) return 'running';
  if (all.length && all.every(x => x === 'done')) return 'done';
  return 'pending';
}

function suiteCellHtml(suite, containers, repeatIndex) {
  const w = suite.warmup_status || 'pending';
  // suite.last_error 主要承载 warmup 错误摘要
  const wErr = suite.last_error || '';
  // 先构建 (repeat, suite, task, stage) → container_name 的索引
  // —— 每个 cell 的 tooltip 末尾会附 ``docker kill <name>`` 救火命令
  const ctrFor = (taskName, stagePrefix) => {
    if (!containers) return null;
    for (const c of containers) {
      if (c.repeat_index !== repeatIndex) continue;
      if (c.suite_name && suite.name && c.suite_name !== suite.name) continue;
      if (taskName != null && c.task_name && c.task_name !== taskName) {
        // task 名经拼音 / 截断后可能不一致；保留 stage 匹配作兜底
        if (!c.task_name.includes(taskName) && !taskName.includes(c.task_name)) continue;
      }
      if (stagePrefix && c.stage && !c.stage.startsWith(stagePrefix)) continue;
      return c.container_name;
    }
    return null;
  };
  const killHint = (name) => name ? `\n\n[copy to kill] docker kill ${name}` : '';
  // warmup 题级 tooltip：列出每个 warmup_task 的状态符号 + 名字 + 可选错误摘要 + 容器名
  const wTasks = Object.values(suite.warmup_tasks || {});
  let wTitle = `warmup [${w}]`;
  let wKill = '';
  if (wTasks.length) {
    const lines = wTasks.map(t => {
      const sym = STATUS_SYM[t.status] || '?';
      const status = t.status || 'pending';
      const err = t.last_error ? `  (${t.last_error})` : '';
      const ctr = (status === 'running' || status === 'retrying')
        ? ctrFor(t.name, 'warmup') : null;
      return `  ${sym} ${t.name.padEnd(8)} [${status}]${err}${ctr ? `\n      docker kill ${ctr}` : ''}`;
    });
    wTitle = `warmup [${w}]\n` + lines.join('\n');
  } else if (wErr) {
    wTitle = `warmup [${w}]: ${wErr}`;
    if (w === 'running' || w === 'retrying') {
      wKill = killHint(ctrFor(null, 'warmup'));
    }
  } else if (w === 'running' || w === 'retrying') {
    wKill = killHint(ctrFor(null, 'warmup'));
  }
  wTitle += wKill;
  // 取所有 holdout 题里的 baseline/evolved 聚合状态 + 第一条错误摘要
  const phaseAgg = (phase) => {
    const tasks = Object.values(suite.holdout_tasks || {});
    const xs = tasks.map(t => (t.phases || {})[phase]?.status || 'pending');
    const errs = tasks
      .map(t => (t.phases || {})[phase]?.last_error)
      .filter(Boolean);
    let st;
    if (!xs.length) st = 'pending';
    else if (xs.includes('failed')) st = 'failed';
    else if (xs.every(x => x === 'done')) st = 'done';
    else if (xs.includes('running') || xs.includes('retrying') || xs.includes('done')) st = 'running';
    else st = 'pending';
    return { st, err: errs.join(' | ') };
  };
  const baseline = phaseAgg('baseline');
  const evolved = phaseAgg('evolved');
  // baseline / evolved 也按题展开 tooltip
  const phaseTooltip = (phase, label, st) => {
    const tasks = Object.values(suite.holdout_tasks || {});
    if (!tasks.length) return `${label} [${st}]`;
    const lines = tasks.map(t => {
      const p = (t.phases || {})[phase];
      const status = p?.status || 'pending';
      const sym = STATUS_SYM[status] || '?';
      const err = p?.last_error ? `  (${p.last_error})` : '';
      const ctr = (status === 'running' || status === 'retrying')
        ? ctrFor(t.name, `holdout/${phase}`) : null;
      return `  ${sym} ${t.name.padEnd(8)} [${status}]${err}${ctr ? `\n      docker kill ${ctr}` : ''}`;
    });
    return `${label} [${st}]\n` + lines.join('\n');
  };
  const cell = (st, sym, title) =>
    `<span class="${st} tip" data-tip="${escapeAttr(title)}">${sym}</span>`;
  const html = [
    cell(w, STATUS_SYM[w] || '?', wTitle),
    cell(baseline.st, STATUS_SYM[baseline.st] || '?',
      phaseTooltip('baseline', 'baseline', baseline.st)),
    cell(evolved.st, STATUS_SYM[evolved.st] || '?',
      phaseTooltip('evolved', 'evolved', evolved.st)),
  ].join(' ');
  return { html };
}

function render() {
  if (!snapshot) return;
  const repeats = snapshot.repeats || [];
  document.getElementById('run-id').textContent = snapshot.run_id || '(no run)';

  // run params 面板：来自 RunPlanEvent.params
  const params = snapshot.params || [];
  const paramsPanel = document.getElementById('params-panel');
  const paramsDiv = document.getElementById('params');
  if (params.length) {
    paramsPanel.style.display = '';
    paramsDiv.innerHTML = params.map(([k, v]) =>
      `<div><span class="pk">${k}</span><span class="pv">${v}</span></div>`
    ).join('');
  } else {
    paramsPanel.style.display = 'none';
  }

  // overall 进度（每个 repeat × suite 计 1 个单元）
  let total = 0, done = 0, running = 0;
  for (const r of repeats) {
    for (const s of Object.values(r.suites || {})) {
      total += 1;
      const ov = suiteOverall(s);
      if (ov === 'done') done += 1;
      else if (ov === 'running') running += 1;
    }
  }
  const pct = total ? Math.round(done * 100 / total) : 0;
  document.getElementById('overall-bar').style.width = pct + '%';
  const elapsed = snapshot.run_started_at ? (Date.now() / 1000 - snapshot.run_started_at) : 0;
  const eta = (done > 0) ? (elapsed * (total - done) / done) : null;
  document.getElementById('overall-stats').textContent =
    `${done}/${total} suites · running ${running} · elapsed ${fmtDuration(elapsed)}` +
    (eta != null ? ` · ETA ${fmtDuration(eta)}` : '');

  // repeats 进度条
  const rDiv = document.getElementById('repeats');
  rDiv.innerHTML = '';
  for (const r of repeats) {
    const suites = Object.values(r.suites || {});
    const total_r = suites.length;
    const done_r = suites.filter(s => suiteOverall(s) === 'done').length;
    const run_r = suites.filter(s => suiteOverall(s) === 'running').length;
    const pct_r = total_r ? Math.round(done_r * 100 / total_r) : 0;
    const row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = `<div style="width:60px">repeat ${r.index}</div>`
      + `<div class="bar"><div style="width:${pct_r}%"></div></div>`
      + `<div class="muted" style="width:140px;text-align:right">${done_r}/${total_r} · running ${run_r}</div>`;
    rDiv.appendChild(row);
  }

  // suites × repeats 栅格
  const hideDone = document.getElementById('hide-done').checked;
  const filter = document.getElementById('filter').value.trim().toLowerCase();
  // 收集所有 suite 名（按 index 排序）
  const allIdx = new Set();
  for (const r of repeats) for (const k of Object.keys(r.suites || {})) allIdx.add(parseInt(k, 10));
  const idxArr = [...allIdx].sort((a, b) => a - b);
  // 表头
  const thead = document.querySelector('#grid thead');
  thead.innerHTML = '<tr><th>suite</th>' + repeats.map(r => `<th>r${r.index}<br><span class="muted">w b e</span></th>`).join('') + '</tr>';
  const tbody = document.querySelector('#grid tbody');
  tbody.innerHTML = '';
  let hiddenDone = 0;
  for (const i of idxArr) {
    // 任意 repeat 的 suite name 都行
    const sample = repeats.map(r => r.suites?.[i]).find(Boolean);
    if (!sample) continue;
    const overall = repeats.map(r => suiteOverall(r.suites?.[i] || {warmup_status:'pending', holdout_tasks:{}}));
    const allDone = overall.every(x => x === 'done');
    if (hideDone && allDone) { hiddenDone += 1; continue; }
    if (filter && !sample.name.toLowerCase().includes(filter)) continue;
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${sample.name}</td>` + repeats.map(r => {
      const c = suiteCellHtml(r.suites?.[i] || {}, snapshot.containers || [], r.index);
      return `<td class="cell">${c.html}</td>`;
    }).join('');
    tbody.appendChild(tr);
  }
  if (hideDone && hiddenDone) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td colspan="${repeats.length + 1}" class="muted done">+ ${hiddenDone} suites done</td>`;
    tbody.appendChild(tr);
  }

  // containers
  const ctrTbody = document.querySelector('#ctr tbody');
  ctrTbody.innerHTML = '';
  const ctrs = (snapshot.containers || []).slice().sort((a, b) => (a.started_at || 0) - (b.started_at || 0));
  document.getElementById('ctr-count').textContent = `(${ctrs.length})`;
  for (const c of ctrs) {
    const up = c.started_at ? fmtDuration(Date.now() / 1000 - c.started_at) : '-';
    const tr = document.createElement('tr');
    const elide = (s) => (s && s.length > 36 ? '…' + s.slice(-35) : (s || '-'));
    // hover 整行显示完整名 + docker kill 命令（方便复制救火）
    const tip = `${c.container_name || ''}\n\ndocker kill ${c.container_name || ''}`;
    tr.className = 'tip';
    tr.setAttribute('data-tip', tip);
    tr.innerHTML = `<td>${elide(c.container_name)}</td>`
      + `<td>${c.repeat_index ?? '-'}</td>`
      + `<td>${c.stage || '-'}</td>`
      + `<td>${c.suite_name || '-'}</td>`
      + `<td>${c.task_name || '-'}</td>`
      + `<td>${up}</td>`;
    ctrTbody.appendChild(tr);
  }

  // recent failures：每条 ErrorRecord 一行
  const errsPanel = document.getElementById('errors-panel');
  const errsTbody = document.querySelector('#errors tbody');
  const errs = snapshot.recent_errors || [];
  document.getElementById('err-count').textContent = `(${errs.length})`;
  if (errs.length) {
    errsPanel.style.display = '';
    errsTbody.innerHTML = '';
    const max = 50;
    for (let i = 0; i < Math.min(errs.length, max); i++) {
      const e = errs[i];
      const ts = e.at ? new Date(e.at * 1000).toLocaleTimeString() : '-';
      const coord = [`r${e.repeat_index}`,
        e.suite_name, e.task_name, e.phase].filter(Boolean).join(' / ');
      const tr = document.createElement('tr');
      tr.innerHTML = `<td class="muted">${ts}</td>`
        + `<td>${e.kind || ''}</td>`
        + `<td>${escapeAttr(coord)}</td>`
        + `<td class="failed" title="${escapeAttr(e.detail || '')}">${escapeAttr(e.detail || '')}</td>`;
      errsTbody.appendChild(tr);
    }
  } else {
    errsPanel.style.display = 'none';
  }
}

// 应用单条事件到本地 snapshot（与服务端 RunStateTracker 大致同步；遇到不熟悉的事件
// 时直接拉一次 /snapshot 兜底）。
async function applyEvent(env) {
  // 简化策略：任何事件都触发一次 snapshot 拉取（只在 5s 内最多一次）。
  await refreshSnapshot();
}

let _snapshotInflight = null;
let _lastSnapshotAt = 0;
async function refreshSnapshot(force = false) {
  const now = Date.now();
  if (!force && _snapshotInflight) return _snapshotInflight;
  if (!force && (now - _lastSnapshotAt) < 800) return;
  _snapshotInflight = fetch('/snapshot').then(r => r.json()).then(s => {
    snapshot = s;
    _lastSnapshotAt = Date.now();
    render();
  }).catch(e => console.error(e)).finally(() => { _snapshotInflight = null; });
  return _snapshotInflight;
}

// 连接状态由 SSE 维护：onopen → live；onerror → disconnected（浏览器自带重连）
function connect() {
  const conn = document.getElementById('conn');
  const es = new EventSource('/events');
  es.onopen = () => { conn.textContent = 'live'; conn.className = 'conn live'; };
  es.onerror = () => { conn.textContent = 'disconnected'; conn.className = 'conn dead'; };
  es.onmessage = (m) => {
    try { const env = JSON.parse(m.data); applyEvent(env); }
    catch (e) { console.warn('bad sse payload', e); }
  };
}

// 控件
document.getElementById('hide-done').addEventListener('change', render);
document.getElementById('filter').addEventListener('input', render);

// 启动
refreshSnapshot(true).then(connect);
// 即使没有事件也每秒重绘一次（计算 elapsed/uptime）
setInterval(() => render(), 1000);
</script>
</body>
</html>
"""


# ---- SSE 客户端注册表 ----------------------------------------------------


class _SSEClient:
    """单个 SSE 客户端的事件队列。"""

    def __init__(self) -> None:
        self.queue: queue.Queue[str | None] = queue.Queue(maxsize=1024)


class _ClientRegistry:
    """SSE 客户端集中注册表（多线程安全）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[_SSEClient] = []

    def add(self) -> _SSEClient:
        c = _SSEClient()
        with self._lock:
            self._clients.append(c)
        return c

    def remove(self, c: _SSEClient) -> None:
        with self._lock:
            try:
                self._clients.remove(c)
            except ValueError:
                pass

    def broadcast(self, payload: str) -> None:
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            try:
                c.queue.put_nowait(payload)
            except queue.Full:
                # 客户端落后太多：踢掉，让它重连后通过 /snapshot 重建状态
                try:
                    c.queue.put_nowait(None)
                except queue.Full:
                    pass


# ---- 主入口：HttpDashboard -----------------------------------------------


class HttpDashboard:
    """HTTP 仪表盘：后台线程跑 ``ThreadingHTTPServer`` + SSE 推送。

    使用方式（在 lift_main 里包一层 contextmanager）::

        dashboard = HttpDashboard(tracker, host="0.0.0.0", port=8765)
        dashboard.start()
        try:
            ...  # 运行 pipeline
        finally:
            dashboard.stop()
    """

    def __init__(
        self,
        tracker: RunStateTracker,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self._tracker = tracker
        self._host = host
        self._port = port
        self._registry = _ClientRegistry()
        self._server: ThreadingHTTPServer | None = None
        self._serve_thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ---- 生命周期 ----

    def start(self) -> None:
        """注册事件订阅、启动后台 HTTP 线程。失败时静默降级（仅 warning）。"""
        try:
            self._server = ThreadingHTTPServer(
                (self._host, self._port), self._make_handler()
            )
        except OSError as exc:
            LOGGER.warning(
                "HttpDashboard cannot bind %s:%d (%s); dashboard disabled.",
                self._host,
                self._port,
                exc,
            )
            self._server = None
            return

        ev.subscribe(self._on_event)
        self._serve_thread = threading.Thread(
            target=self._server.serve_forever,
            name="lift-http-dashboard",
            daemon=True,
        )
        self._serve_thread.start()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="lift-http-dashboard-keepalive",
            daemon=True,
        )
        self._keepalive_thread.start()
        LOGGER.info(
            "HTTP status dashboard listening on http://%s:%d",
            self._host,
            self._port,
        )

    def stop(self) -> None:
        """注销事件订阅、关闭 HTTP 服务并等待线程退出。"""
        ev.unsubscribe(self._on_event)
        self._stop_event.set()
        # 通知所有 SSE 客户端结束
        self._registry.broadcast(_SENTINEL_DONE)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        for t in (self._serve_thread, self._keepalive_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)

    # ---- 事件 → SSE -----------------------------------------------------

    def _on_event(self, event: object) -> None:
        try:
            data = json.dumps(_event_payload(event), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            LOGGER.debug("HttpDashboard cannot serialize event: %s", exc)
            return
        self._registry.broadcast(_format_sse(data))

    def _keepalive_loop(self) -> None:
        """每 ``_SSE_KEEPALIVE_SECONDS`` 秒发一次心跳（注释行），防止反向代理断连。"""
        while not self._stop_event.wait(timeout=_SSE_KEEPALIVE_SECONDS):
            self._registry.broadcast(": keepalive\n\n")

    # ---- HTTP handler ---------------------------------------------------

    def _make_handler(self):
        dashboard = self
        registry = self._registry
        tracker = self._tracker

        class Handler(BaseHTTPRequestHandler):
            # 重写日志：默认会打到 stderr，干扰 TUI / 日志
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                LOGGER.debug("http_dashboard %s - %s", self.address_string(), format % args)

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/" or path == "/index.html":
                    self._send_html(_INDEX_HTML)
                elif path == "/snapshot":
                    snapshot = tracker.snapshot()
                    body = json.dumps(_snapshot_payload(snapshot), ensure_ascii=False)
                    self._send_json(body)
                elif path == "/events":
                    self._serve_sse(registry, tracker)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "Not found")

            def _send_html(self, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)

            def _send_json(self, body: str) -> None:
                data = body.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            def _serve_sse(
                self, registry: _ClientRegistry, tracker: RunStateTracker
            ) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")  # 关掉 nginx 缓冲
                self.end_headers()

                client = registry.add()
                # 连接建立后先推一份 snapshot，前端可立即重建状态
                try:
                    snap_payload = json.dumps(
                        {
                            "type": "Snapshot",
                            "data": _snapshot_payload(tracker.snapshot()),
                        },
                        ensure_ascii=False,
                    )
                    self.wfile.write(_format_sse(snap_payload).encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    registry.remove(client)
                    return

                try:
                    while not dashboard._stop_event.is_set():
                        try:
                            payload = client.queue.get(timeout=1.0)
                        except queue.Empty:
                            continue
                        if payload is None or payload == _SENTINEL_DONE:
                            break
                        try:
                            self.wfile.write(payload.encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            break
                finally:
                    registry.remove(client)

        return Handler


_SENTINEL_DONE = "__done__"


def _format_sse(data: str) -> str:
    """SSE 协议格式化：``data:`` 行 + 空行结尾。多行 data 自动按行拆分。"""
    lines = data.splitlines() or [""]
    return "".join(f"data: {ln}\n" for ln in lines) + "\n"
