# chat_blueprint.py
"""
实时聊天蓝图（内存版）

功能：
- 所有消息保存在服务器进程内存中（不落库）
- 客户端定时轮询接口拉取增量消息
- 点击消息自动复制内容到剪贴板
- 快捷短语按钮：点击后自动粘贴到输入框并发送
- 作为 Flask Blueprint 被其他应用注册使用

用法：
    from flask import Flask
    from chat_blueprint import bp as chat_bp

    app = Flask(__name__)
    app.register_blueprint(chat_bp)                       # 默认挂载在 /chat/
    # app.register_blueprint(chat_bp, url_prefix="/live")  # 也可自定义前缀

然后访问 http://127.0.0.1:5000/chat/
"""

from __future__ import annotations

import threading
import time
from collections import deque

from flask import Blueprint, jsonify, render_template_string, request, url_for

# --------------------------------------------------------------------------
# 蓝图与内存数据结构
# --------------------------------------------------------------------------

bp = Blueprint("chat", __name__, url_prefix="/chat")

MAX_MESSAGES = 500      # 内存中最多保留的消息条数（滚动丢弃旧的）
MAX_NAME_LEN = 20       # 昵称最大长度
MAX_TEXT_LEN = 1000     # 单条消息最大长度

# 快捷回复短语：点击按钮后自动粘贴到输入框并发送。按需要自由增删。
QUICK_REPLIES = [
    "你好 👋",
    "收到 ✅",
    "稍等，我看一下",
    "好的，没问题",
    "感谢！",
    "哈哈 😂",
]

_lock = threading.Lock()          # 多线程（多请求）下保护内存数据
_messages: deque[dict] = deque(maxlen=MAX_MESSAGES)
_next_id: int = 1                 # 自增消息 ID


def _add_message(name: str, text: str) -> dict:
    """线程安全地写入一条消息，返回消息字典。"""
    global _next_id
    with _lock:
        msg = {
            "id": _next_id,
            "name": name,
            "text": text,
            "ts": time.time(),
        }
        _next_id += 1
        _messages.append(msg)
    return msg


def _snapshot(since_id: int) -> tuple[list[dict], int]:
    """取出 id > since_id 的所有消息，以及当前最大 id。"""
    with _lock:
        msgs = [m for m in _messages if m["id"] > since_id]
        last_id = _next_id - 1
    return msgs, last_id


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

@bp.route("/")
def index():
    """聊天页面。"""
    return render_template_string(PAGE_HTML, quick_replies=QUICK_REPLIES)


@bp.get("/api/messages")
def api_messages():
    """
    增量拉取消息。
    GET /api/messages?since=<已收到的最大 id>
    返回 {"messages": [...], "last_id": N}
    """
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    if since < 0:
        since = 0

    msgs, last_id = _snapshot(since)
    return jsonify({"messages": msgs, "last_id": last_id})


@bp.post("/api/messages")
def api_post_message():
    """
    发送消息。
    POST /api/messages  JSON: {"name": "...", "text": "..."}
    也支持表单提交。
    """
    data = request.get_json(silent=True) or request.form

    name = (data.get("name") or "").strip() or "匿名"
    text = (data.get("text") or "").strip()

    if not text:
        return jsonify({"error": "消息内容不能为空"}), 400

    name = name[:MAX_NAME_LEN]
    text = text[:MAX_TEXT_LEN]

    msg = _add_message(name, text)
    return jsonify(msg), 201


# --------------------------------------------------------------------------
# 页面（HTML + CSS + JS）
# --------------------------------------------------------------------------

PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>实时聊天室</title>
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

  /* ---------------- 消息区 ---------------- */
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
    cursor: pointer;                 /* 点击可复制 */
    transition: background .15s, transform .08s;
    animation: pop .15s ease-out;
  }
  .msg:hover  { background: #262e3c; }
  .msg:active { transform: scale(.985); }
  .msg.me { background: #1d3a55; align-self: flex-end; }
  .msg.me:hover { background: #24486a; }

  /* 复制成功徽标 */
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

  /* ---------------- 快捷短语 ---------------- */
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

  /* ---------------- 输入区 ---------------- */
  form { display: flex; gap: 8px; }
  input {
    background: #161a22; border: 1px solid #232a37; color: inherit;
    padding: 10px 12px; border-radius: 10px; font: inherit; outline: none;
  }
  input:focus { border-color: #3d6ea8; }
  #name { width: 92px; flex: none; }
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
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>实时聊天室</h1>
    <span id="status">连接中…</span>
  </header>

  <div id="messages"><div class="empty">还没有消息，来说第一句吧 ~</div></div>

  <!-- 快捷短语：点击自动粘贴并发送 -->
  <div class="quick" id="quick"></div>

  <form id="form" autocomplete="off">
    <input id="name" maxlength="20" placeholder="昵称">
    <input id="text" maxlength="1000" placeholder="说点什么…（点击上方气泡可复制消息）" required>
    <button id="send" type="submit">发送</button>
  </form>
</div>

<script>
(function () {
  "use strict";

  const API      = {{ url_for('.api_messages')|tojson }};
  const QUICK    = {{ quick_replies|tojson }};

  const box      = document.getElementById("messages");
  const form     = document.getElementById("form");
  const nameEl   = document.getElementById("name");
  const textEl   = document.getElementById("text");
  const sendEl   = document.getElementById("send");
  const status   = document.getElementById("status");
  const quickBox = document.getElementById("quick");

  const POLL_MS = 1000;      // 轮询间隔（毫秒）
  const COPY_HL = 1100;      // “已复制”徽标显示时长（毫秒）

  let lastId   = 0;          // 已收到的最大消息 id
  let myName   = localStorage.getItem("chat_name") || "";
  let inFlight = false;      // 拉取中标记
  let sending  = false;      // 发送中标记
  let timer    = null;

  nameEl.value = myName;

  /* ------------------------------------------------------------------ */
  /* 工具函数                                                           */
  /* ------------------------------------------------------------------ */

  function setStatus(ok) {
    status.classList.toggle("on", ok);
    status.classList.toggle("off", !ok);
    status.textContent = ok ? "已连接" : "连接断开，重试中…";
  }

  function fmtTime(ts) {
    try { return new Date(ts * 1000).toLocaleTimeString(); }
    catch (e) { return ""; }
  }

  function isNearBottom() {
    return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  }

  function scrollToBottom() {
    box.scrollTop = box.scrollHeight;
  }

  /* ------------------------------------------------------------------ */
  /* 渲染消息                                                           */
  /* ------------------------------------------------------------------ */

  function appendMessage(m) {
    const empty = box.querySelector(".empty");
    if (empty) empty.remove();

    const el = document.createElement("div");
    el.className = "msg" + (myName && m.name === myName ? " me" : "");
    el.dataset.raw = m.text;            // 复制时取这份原始文本
    el.title = "点击复制这条消息";

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = m.name + " · " + fmtTime(m.ts);

    const body = document.createElement("div");
    body.textContent = m.text;          // textContent 天然防 XSS

    el.appendChild(meta);
    el.appendChild(body);
    box.appendChild(el);
  }

  /* ------------------------------------------------------------------ */
  /* 点击消息 → 复制到剪贴板                                            */
  /* ------------------------------------------------------------------ */

  async function copyText(text) {
    // 优先使用异步剪贴板 API（需要 HTTPS 或 localhost）
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    // 退化方案：临时 textarea + execCommand
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

    // 用户正在划词选择时，不触发整条复制
    const sel = window.getSelection();
    if (sel && sel.toString().trim()) return;

    const raw = el.dataset.raw || el.textContent || "";
    if (!raw.trim()) return;

    try {
      await copyText(raw);
      flashCopied(el);
    } catch (err) {
      // 复制失败时把内容塞进输入框，方便手动 Ctrl+C
      textEl.value = raw;
      textEl.focus();
      textEl.select();
      setStatus(false);
    }
  });

  /* ------------------------------------------------------------------ */
  /* 拉取增量消息                                                       */
  /* ------------------------------------------------------------------ */

  async function poll() {
    if (inFlight) return;
    inFlight = true;
    try {
      const res = await fetch(API + "?since=" + lastId, { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);

      const data = await res.json();
      const list = data.messages || [];

      if (list.length) {
        const stick = isNearBottom();       // 只有本来就在底部才自动滚动
        for (const m of list) appendMessage(m);
        if (stick) scrollToBottom();
      }
      if (typeof data.last_id === "number" && data.last_id > lastId) {
        lastId = data.last_id;
      }
      setStatus(true);
    } catch (err) {
      setStatus(false);
    } finally {
      inFlight = false;
      clearTimeout(timer);
      timer = setTimeout(poll, POLL_MS);
    }
  }

  /* ------------------------------------------------------------------ */
  /* 发送消息                                                           */
  /* ------------------------------------------------------------------ */

  async function sendMessage(text) {
    text = (text || "").trim();
    if (!text || sending) return false;

    myName = nameEl.value.trim() || "匿名";
    localStorage.setItem("chat_name", myName);

    sending = true;
    sendEl.disabled = true;
    setQuickDisabled(true);

    let ok = false;
    try {
      const res = await fetch(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: myName, text: text })
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      ok = true;
      setStatus(true);
    } catch (err) {
      setStatus(false);
    } finally {
      sending = false;
      sendEl.disabled = false;
      setQuickDisabled(false);
      textEl.focus();
      poll();               // 立刻补拉一次，减少等待感
    }
    return ok;
  }

  // 表单提交
  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    const text = textEl.value.trim();
    if (!text) return;

    textEl.value = "";
    const ok = await sendMessage(text);
    if (!ok) {
      textEl.value = text;   // 失败把内容还回去
      alert("发送失败，请检查网络后重试");
    }
  });

  /* ------------------------------------------------------------------ */
  /* 快捷短语按钮：点击 → 自动粘贴 → 自动发送                            */
  /* ------------------------------------------------------------------ */

  const quickBtns = [];

  function setQuickDisabled(disabled) {
    for (const b of quickBtns) b.disabled = disabled;
  }

  async function quickSend(text) {
    if (sending) return;

    // 1) 自动“粘贴”到输入框，并闪一下作为视觉反馈
    textEl.value = text;
    textEl.classList.remove("pasted");
    void textEl.offsetWidth;              // 强制重排以重启动画
    textEl.classList.add("pasted");
    setTimeout(function () { textEl.classList.remove("pasted"); }, 500);

    // 2) 自动发送；成功后清空输入框，失败则保留内容便于手动重试
    const ok = await sendMessage(text);
    if (ok) textEl.value = "";
  }

  QUICK.forEach(function (q) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = q;
    b.title = "点击自动发送：" + q;
    b.addEventListener("click", function () { quickSend(q); });
    quickBox.appendChild(b);
    quickBtns.push(b);
  });

  /* ------------------------------------------------------------------ */
  /* 其他                                                               */
  /* ------------------------------------------------------------------ */

  // 页面重新可见时立刻同步一次（后台标签页被节流后补拉）
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });

  textEl.focus();
  poll();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# 可选：对外暴露的注册函数
# --------------------------------------------------------------------------

def init_app(app, url_prefix: str = "/chat") -> None:
    """把聊天蓝图注册到 Flask 应用上。"""
    app.register_blueprint(bp, url_prefix=url_prefix)