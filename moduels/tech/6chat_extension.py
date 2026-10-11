# chat2.py
"""
PocketBase 实时聊天蓝图（单体文件：后端 + 前端）

依赖环境变量：
    POCKETBASE_URL / PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD

挂载：
    from chat2 import chat2_bp
    app.register_blueprint(chat2_bp)   # 默认挂在 /chat2/
"""

from __future__ import annotations

import os
import secrets
import threading
import traceback
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, Response, jsonify, request

# --------------------------------------------------------------------------
# 蓝图
# --------------------------------------------------------------------------

CHAT2_PREFIX = "/chat2"
chat2_bp = Blueprint("chat2", __name__, url_prefix=CHAT2_PREFIX)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

COLLECTION = "demo2"
FIELD_TEXT = "text"
FIELD_TIME = "msg_time"

MAX_TEXT_LEN = 1000
PAGE_SIZE = 100

QUICK_BUTTONS = [
    "📋 发送剪贴板内容",
]

CST = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _new_field_id() -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(15))


def _field(name: str, type_: str, **extra) -> dict:
    return {"id": _new_field_id(), "name": name, "type": type_, **extra}


# --------------------------------------------------------------------------
# PocketBase 客户端
# --------------------------------------------------------------------------

class PocketBaseClient:
    def __init__(self) -> None:
        try:
            self.base_url = os.environ["POCKETBASE_URL"].rstrip("/")
            self.email = os.environ["PB_ADMIN_EMAIL"]
            self.password = os.environ["PB_ADMIN_PASSWORD"]
        except KeyError as e:
            raise RuntimeError(
                f"缺少环境变量 {e.args[0]}，"
                f"请设置 POCKETBASE_URL / PB_ADMIN_EMAIL / PB_ADMIN_PASSWORD"
            ) from None

        self._token: str | None = None
        self._token_exp: datetime | None = None
        self._lock = threading.Lock()

    def _auth(self) -> None:
        url = f"{self.base_url}/api/collections/_superusers/auth-with-password"
        try:
            r = requests.post(
                url,
                json={"identity": self.email, "password": self.password},
                timeout=10,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"连接 PocketBase 失败（{url}）：{e}") from e

        if r.status_code >= 400:
            raise RuntimeError(
                f"PocketBase 认证失败 [{r.status_code}] {url}\n"
                f"email={self.email!r}\n"
                f"响应：{r.text[:500]}"
            )

        data = r.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"认证响应里没有 token：{data}")

        self._token = token
        self._token_exp = datetime.now() + timedelta(hours=12)

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._token_exp is None or datetime.now() >= self._token_exp:
                self._auth()
            assert self._token is not None
            return self._token

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.token()}"

        try:
            resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
        except requests.RequestException as e:
            raise RuntimeError(f"请求 PocketBase 失败 {method} {url}：{e}") from e

        if resp.status_code == 401:
            with self._lock:
                self._auth()
            headers["Authorization"] = f"Bearer {self.token()}"
            resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)

        return resp

    def ensure_collection(self, name: str, fields: list) -> None:
        r = self.request("GET", f"/api/collections/{name}")

        if r.status_code == 200:
            info = r.json()
            existing_fields = info.get("fields", []) or []
            existing_names = {f.get("name") for f in existing_fields}

            missing = [f for f in fields if f["name"] not in existing_names]
            if not missing:
                return

            merged = existing_fields + missing
            pr = self.request(
                "PATCH",
                f"/api/collections/{name}",
                json={"fields": merged},
            )
            if pr.status_code >= 400:
                raise RuntimeError(
                    f"集合 {name!r} 缺少字段 {[f['name'] for f in missing]}，"
                    f"自动补齐失败 [{pr.status_code}]：{pr.text[:800]}"
                )
            return

        if r.status_code != 404:
            raise RuntimeError(
                f"查询集合 {name!r} 失败 [{r.status_code}]：{r.text[:500]}"
            )

        payload = {"name": name, "type": "base", "fields": fields}
        r = self.request("POST", "/api/collections", json=payload)
        if r.status_code >= 400:
            raise RuntimeError(
                f"创建集合 {name!r} 失败 [{r.status_code}]：{r.text[:800]}\n"
                f"payload={payload}"
            )


_pb: PocketBaseClient | None = None
_pb_lock = threading.Lock()
_pb_init_error: str | None = None


def _get_pb() -> PocketBaseClient:
    global _pb, _pb_init_error
    if _pb is not None:
        return _pb
    with _pb_lock:
        if _pb is None:
            try:
                _pb = PocketBaseClient()
                _pb_init_error = None
            except Exception as e:
                _pb_init_error = str(e)
                raise
    return _pb


_collection_ready = False
_ready_lock = threading.Lock()


def _ensure_ready() -> None:
    global _collection_ready
    if _collection_ready:
        return
    with _ready_lock:
        if _collection_ready:
            return
        _get_pb().ensure_collection(
            COLLECTION,
            fields=[
                _field(FIELD_TEXT, "text", required=True, max=MAX_TEXT_LEN),
                _field(FIELD_TIME, "date"),
            ],
        )
        _collection_ready = True


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------

def _now_utc_iso() -> str:
    """当前 UTC 时间，ISO 格式：2026-01-01T12:00:00.123Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _normalize_dt(v: str | None) -> str:
    """
    把客户端传来的时间统一成 UTC ISO。
    - 不带时区后缀：按大陆时间解释（UTC+8）
    - 带 Z：UTC
    - 带 +08:00 / +0800：大陆时间
    - 无法解析：退回服务器当前时间
    """
    if not v:
        return _now_utc_iso()

    s = v.strip().replace(" ", "T")
    tz: timezone = CST

    if s.endswith("Z"):
        s = s[:-1]
        tz = timezone.utc
    elif s.endswith("+08:00"):
        s = s[:-6]
    elif s.endswith("+0800"):
        s = s[:-5]

    if "." not in s:
        s += ".000"

    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=tz)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        except ValueError:
            continue
    return _now_utc_iso()


def _serialize(rec: dict) -> dict:
    """对外暴露 text / datetime。datetime 为 T 分隔的 UTC ISO。"""
    dt = rec.get(FIELD_TIME) or ""
    if dt:
        dt = dt.replace(" ", "T")
        if not dt.endswith("Z"):
            dt += "Z"
    return {
        "id": rec.get("id"),
        "text": rec.get(FIELD_TEXT, ""),
        "datetime": dt,
    }


# --------------------------------------------------------------------------
# 错误响应
# --------------------------------------------------------------------------

def _err(stage: str, exc: Exception | str, status: int = 500):
    msg = str(exc)
    tb = traceback.format_exc()
    print(f"\n=== [chat2] {stage} failed ===\n{msg}\n{tb}\n", flush=True)
    return jsonify({"stage": stage, "error": msg}), status


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

@chat2_bp.get("/")
def index():
    return Response(PAGE_HTML, mimetype="text/html")


@chat2_bp.post("/api/poll")
def api_poll():
    """
    轮询消息（POST，避免任何中间层缓存）。

    body（JSON）：
        {}                          → 首屏，返回最近 PAGE_SIZE 条
        {"since": "2026-01-01T12:00:00.123Z"}  → 增量

    响应：
        首屏 / 有增量：{"ok": true, "has_new": true, "messages": [...]}
        无新消息：    {"ok": true, "has_new": false}
    """
    try:
        _ensure_ready()
    except Exception as e:
        return _err("ensure_ready", e)

    body = request.get_json(silent=True) or {}
    since = (body.get("since") or "").strip()

    if since:
        since_utc = _normalize_dt(since)
        pb_since = since_utc.replace("T", " ").rstrip("Z")
        params = {
            "sort": FIELD_TIME,
            "perPage": PAGE_SIZE,
            "filter": f"{FIELD_TIME} > '{pb_since}Z'",
        }
    else:
        params = {"sort": f"-{FIELD_TIME}", "perPage": PAGE_SIZE}

    try:
        r = _get_pb().request(
            "GET",
            f"/api/collections/{COLLECTION}/records",
            params=params,
        )
    except Exception as e:
        return _err("request", e)

    if r.status_code >= 400:
        return _err("pb_error", f"PocketBase 返回 [{r.status_code}]：{r.text[:800]}")

    try:
        items = r.json().get("items", [])
    except Exception as e:
        return _err("json_decode", f"{e} / body={r.text[:500]}")

    if since and not items:
        return jsonify({"ok": True, "has_new": False})

    if not since:
        items.reverse()

    return jsonify({
        "ok": True,
        "has_new": True,
        "messages": [_serialize(i) for i in items],
    })


@chat2_bp.post("/api/messages")
def api_post_message():
    """发送消息。body: {"text": "..."}"""
    try:
        _ensure_ready()
    except Exception as e:
        return _err("ensure_ready", e)

    data = request.get_json(silent=True) or request.form
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "消息内容不能为空"}), 400
    text = text[:MAX_TEXT_LEN]

    payload = {
        FIELD_TEXT: text,
        FIELD_TIME: _now_utc_iso().replace("T", " "),
    }

    try:
        r = _get_pb().request(
            "POST",
            f"/api/collections/{COLLECTION}/records",
            json=payload,
        )
    except Exception as e:
        return _err("request", e)

    if r.status_code >= 400:
        return _err(
            "pb_error",
            f"PocketBase 返回 [{r.status_code}]：{r.text[:800]} / payload={payload}",
        )

    try:
        rec = r.json()
    except Exception as e:
        return _err("json_decode", f"{e} / body={r.text[:500]}")

    return jsonify(_serialize(rec)), 201


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat2 实时聊天室</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", Roboto,
                 "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #0f1116;
    color: #e6e8ee;
  }
  .wrap {
    height: 100%;
    max-width: 780px;
    margin: 0 auto;
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  header { display: flex; align-items: center; gap: 10px; }
  h1 { font-size: 17px; margin: 0; font-weight: 600; }
  #status {
    font-size: 12px; padding: 2px 9px; border-radius: 99px;
    background: #1e2430; color: #8b93a7; transition: .2s;
  }
  #status.on  { background: #12301f; color: #5ee08a; }
  #status.off { background: #3a1d1d; color: #ff8b8b; }

  #messages {
    flex: 1;
    overflow-y: auto;
    background: #161a22;
    border: 1px solid #232a37;
    border-radius: 12px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .msg {
    position: relative;
    background: #1e2430;
    border-radius: 10px;
    padding: 7px 11px;
    max-width: 80%;
    align-self: flex-start;
    word-break: break-word;
    white-space: pre-wrap;
    cursor: pointer;
    transition: background .15s, transform .08s;
    animation: pop .15s ease-out;
  }
  .msg:hover  { background: #262e3c; }
  .msg:active { transform: scale(.985); }

  .msg.copied::after {
    content: "已复制";
    position: absolute;
    top: -9px; right: 8px;
    font-size: 10px;
    line-height: 1.5;
    padding: 0 7px;
    border-radius: 99px;
    background: #2f6fd0;
    color: #fff;
    white-space: nowrap;
    box-shadow: 0 2px 6px rgba(0,0,0,.35);
    animation: pop .15s ease-out;
  }
  .msg.copied { background: #24486a; }

  .meta { font-size: 11px; color: #8b93a7; margin-bottom: 2px; }
  .empty { color: #5d6577; font-size: 13px; margin: auto; }
  @keyframes pop { from { opacity: 0; transform: translateY(4px); } }

  .quick { display: flex; flex-wrap: wrap; gap: 6px; }
  .quick button {
    background: #1e2430;
    color: #c3cad9;
    border: 1px solid #2b3444;
    border-radius: 99px;
    padding: 5px 12px;
    font: inherit;
    font-size: 12px;
    line-height: 1.6;
    cursor: pointer;
    transition: .15s;
    -webkit-tap-highlight-color: transparent;
  }
  .quick button:hover  { background: #26303f; border-color: #3d6ea8; color: #fff; }
  .quick button:active { transform: scale(.96); }
  .quick button:disabled { opacity: .45; cursor: default; }
  .quick button.waiting {
    border-color: #d09a2f;
    color: #ffd479;
    animation: breathe 1.2s ease-in-out infinite;
  }
  @keyframes breathe { 50% { opacity: .55; } }

  form { display: flex; gap: 8px; }
  input {
    background: #161a22; border: 1px solid #232a37; color: inherit;
    padding: 10px 12px; border-radius: 10px; font: inherit; outline: none;
  }
  input:focus { border-color: #3d6ea8; }
  #text { flex: 1; min-width: 0; }
  #text.pasted { animation: pasteFlash .5s ease-out; }
  @keyframes pasteFlash {
    0%   { background: #24486a; border-color: #3d6ea8; }
    100% { background: #161a22; border-color: #232a37; }
  }
  button[type="submit"] {
    background: #2f6fd0; color: #fff; border: 0; border-radius: 10px;
    padding: 0 18px; font: inherit; cursor: pointer;
  }
  button[type="submit"]:hover { background: #3a80e8; }
  button[type="submit"]:disabled { opacity: .5; cursor: default; }

  #toast {
    position: fixed;
    left: 50%;
    bottom: 92px;
    transform: translateX(-50%) translateY(10px);
    background: #232a37;
    color: #e6e8ee;
    border: 1px solid #2f3a4c;
    font-size: 13px;
    line-height: 1.5;
    padding: 8px 16px;
    border-radius: 99px;
    max-width: 88vw;
    text-align: center;
    opacity: 0;
    pointer-events: none;
    transition: opacity .2s, transform .2s;
    z-index: 50;
    box-shadow: 0 6px 20px rgba(0,0,0,.4);
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Chat2 实时聊天室</h1>
    <span id="status">连接中…</span>
  </header>

  <div id="messages"><div class="empty">还没有消息，来说第一句吧 ~</div></div>

  <div class="quick" id="quick"></div>

  <form id="form" autocomplete="off">
    <input id="text" maxlength="1000"
           placeholder="说点什么…（点击上方气泡可复制消息）" required>
    <button id="send" type="submit">发送</button>
  </form>
</div>

<div id="toast"></div>

<script>
(function () {
  "use strict";

  const API_SEND = "api/messages";   // POST 发消息
  const API_POLL = "api/poll";       // POST 轮询
  const QUICK    = __QUICK_BUTTONS__;

  const box      = document.getElementById("messages");
  const form     = document.getElementById("form");
  const textEl   = document.getElementById("text");
  const sendEl   = document.getElementById("send");
  const status   = document.getElementById("status");
  const quickBox = document.getElementById("quick");
  const toastEl  = document.getElementById("toast");

  const POLL_MS        = 1000;    // 前台 1s
  const POLL_MS_HIDDEN = 10000;   // 后台 10s
  const COPY_HL        = 1100;

  const seenIds  = new Set();
  let   lastTime = "";     // 已渲染消息里最新的一条 datetime（UTC ISO）
  let   inFlight = false;
  let   sending  = false;
  let   pendingPaste = false;
  let   timer      = null;
  let   toastTimer = null;

  function setStatus(ok) {
    status.classList.toggle("on", ok);
    status.classList.toggle("off", !ok);
    status.textContent = ok ? "已连接" : "连接断开，重试中…";
  }

  // 显示为大陆时间（UTC+8），不受用户本地时区影响
  function fmtTime(s) {
    try {
      const d = new Date(String(s || "").replace(" ", "T"));
      if (isNaN(d.getTime())) return s || "";
      const cst = new Date(d.getTime() + 8 * 3600 * 1000);
      const pad = (n) => String(n).padStart(2, "0");
      return pad(cst.getUTCHours()) + ":"
           + pad(cst.getUTCMinutes()) + ":"
           + pad(cst.getUTCSeconds());
    } catch (e) {
      return s || "";
    }
  }

  function isNearBottom() {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  }

  function scrollToBottom() {
    box.scrollTop = box.scrollHeight;
  }

  function toast(msg, ms) {
    ms = ms || 2200;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      toastEl.classList.remove("show");
    }, ms);
  }

  function flashPaste() {
    textEl.classList.remove("pasted");
    void textEl.offsetWidth;
    textEl.classList.add("pasted");
    setTimeout(function () { textEl.classList.remove("pasted"); }, 500);
  }

  function appendMessage(m) {
    const empty = box.querySelector(".empty");
    if (empty) empty.remove();

    const el = document.createElement("div");
    el.className = "msg";
    el.dataset.raw = m.text;
    el.title = "点击复制这条消息";

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = fmtTime(m.datetime);

    const body = document.createElement("div");
    body.textContent = m.text;

    el.appendChild(meta);
    el.appendChild(body);
    box.appendChild(el);
  }

  function mergeRecord(m, forceScroll) {
    if (!m || !m.id) return false;
    if (seenIds.has(m.id)) return false;

    seenIds.add(m.id);
    const stick = isNearBottom();
    appendMessage(m);

    if (m.datetime && m.datetime > lastTime) {
      lastTime = m.datetime;
    }
    if (forceScroll || stick) scrollToBottom();
    return true;
  }

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText
        && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (!ok) throw new Error("copy failed");
    return true;
  }

  function flashCopied(el) {
    el.classList.add("copied");
    setTimeout(function () { el.classList.remove("copied"); }, COPY_HL);
  }

  box.addEventListener("click", async function (e) {
    const el = e.target.closest(".msg");
    if (!el) return;

    const sel = window.getSelection();
    if (sel && sel.toString().trim()) return;

    const raw = el.dataset.raw || el.textContent || "";
    if (!raw.trim()) return;

    try {
      await copyText(raw);
      flashCopied(el);
    } catch (err) {
      textEl.value = raw;
      textEl.focus();
      textEl.select();
      toast("复制失败，已填入输入框，可手动复制");
    }
  });

  function scheduleNextPoll() {
    clearTimeout(timer);
    const delay = document.hidden ? POLL_MS_HIDDEN : POLL_MS;
    timer = setTimeout(poll, delay);
  }

  // ---------------- 轮询（POST） ----------------
  async function poll() {
    if (inFlight) return;
    inFlight = true;

    try {
      const res = await fetch(API_POLL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ since: lastTime || null }),
      });
      if (!res.ok) {
        let detail = "";
        try { detail = JSON.stringify(await res.json()); } catch (e) {}
        console.error("[chat2] poll failed", res.status, detail);
        throw new Error("HTTP " + res.status);
      }

      const data = await res.json();

      // 无新消息：后端返回 {has_new:false}，什么都不做
      if (data && data.has_new === false) {
        setStatus(true);
        return;
      }

      const list = (data && data.messages) || [];
      for (const m of list) mergeRecord(m, false);

      setStatus(true);
    } catch (err) {
      setStatus(false);
    } finally {
      inFlight = false;
      scheduleNextPoll();
    }
  }

  // ---------------- 发送消息 ----------------
  async function sendMessage(text) {
    text = (text || "").trim();
    if (!text || sending) return false;

    sending = true;
    sendEl.disabled = true;
    setQuickDisabled(true);

    let ok = false;
    try {
      const res = await fetch(API_SEND, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text })
      });
      if (!res.ok) {
        let detail = "";
        try { detail = JSON.stringify(await res.json()); } catch (e) {}
        console.error("[chat2] send failed", res.status, detail);
        throw new Error("HTTP " + res.status);
      }

      // 直接本地渲染新建记录，并把 lastTime 推进
      const rec = await res.json();
      mergeRecord(rec, true);

      ok = true;
      setStatus(true);
    } catch (err) {
      setStatus(false);
    } finally {
      sending = false;
      sendEl.disabled = false;
      setQuickDisabled(false);
    }
    return ok;
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const text = textEl.value.trim();
    if (!text) return;

    textEl.value = "";
    const ok = await sendMessage(text);
    if (!ok) {
      textEl.value = text;
      toast("发送失败，请检查网络后重试");
    }
  });

  // ---------------- 快捷按钮 ----------------
  const quickBtns = [];

  function setQuickDisabled(disabled) {
    for (const b of quickBtns) b.disabled = !!disabled;
  }

  function setQuickWaiting(waiting) {
    for (const b of quickBtns) b.classList.toggle("waiting", !!waiting);
  }

  async function readClipboardText() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      throw new Error("Clipboard API 不可用");
    }
    if (!window.isSecureContext) {
      throw new Error("非安全上下文（需 HTTPS 或 localhost）");
    }
    return await navigator.clipboard.readText();
  }

  async function pasteAndSend(text) {
    text = (text || "").trim();
    if (!text) {
      toast("剪贴板是空的，没有可发送的内容");
      return false;
    }

    textEl.value = text;
    flashPaste();

    const ok = await sendMessage(text);
    if (ok) {
      textEl.value = "";
      pendingPaste = false;
      setQuickWaiting(false);
    } else {
      toast("发送失败，内容已保留在输入框，可手动重试");
    }
    return ok;
  }

  async function quickSendFromClipboard() {
    if (sending) return;

    let text = "";
    try {
      text = await readClipboardText();
    } catch (err) {
      pendingPaste = true;
      setQuickWaiting(true);
      textEl.value = "";
      textEl.focus();
      toast("浏览器不允许直接读取剪贴板，请按 Ctrl/⌘+V 粘贴，将自动发送", 4000);
      return;
    }

    await pasteAndSend(text);
  }

  textEl.addEventListener("paste", function (e) {
    if (!pendingPaste) return;
    const cd = e.clipboardData || window.clipboardData;
    if (!cd) return;
    const t = cd.getData("text");
    if (!t || !t.trim()) return;

    e.preventDefault();
    pendingPaste = false;
    pasteAndSend(t);
  });

  textEl.addEventListener("input", function () {
    if (pendingPaste && textEl.value.trim()) {
      pendingPaste = false;
      setQuickWaiting(false);
    }
  });

  textEl.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && pendingPaste) {
      pendingPaste = false;
      setQuickWaiting(false);
      toast("已取消", 1200);
    }
  });

  QUICK.forEach(function (label) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.title = "点击读取剪贴板并自动发送";
    b.addEventListener("click", quickSendFromClipboard);
    quickBox.appendChild(b);
    quickBtns.push(b);
  });

  // ---------------- 可见性调度 ----------------
  document.addEventListener("visibilitychange", function () {
    clearTimeout(timer);
    if (document.hidden) {
      scheduleNextPoll();
    } else {
      poll();   // 切回前台立即补一次
    }
  });

  textEl.focus();
  poll();
})();
</script>
</body>
</html>
"""

PAGE_HTML = PAGE_HTML.replace(
    "__QUICK_BUTTONS__",
    repr(QUICK_BUTTONS).replace("'", '"'),
)