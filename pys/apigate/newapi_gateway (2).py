from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.responses import Response


# ============================================================
# 基础配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("GATEWAY_DB", BASE_DIR / "gateway.db"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "4000"))
TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "120"))
MODEL_REFRESH_TTL = float(os.getenv("MODEL_REFRESH_TTL", "300"))
DEBUG_PROXY = os.getenv("DEBUG_PROXY", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


# ============================================================
# 日志
# ============================================================


def debug(message: str) -> None:
    if DEBUG_PROXY:
        print(f"[DEBUG] {message}", flush=True)


def debug_separator(title: str = "") -> None:
    if not DEBUG_PROXY:
        return
    print("\n" + "=" * 80, flush=True)
    if title:
        print(title, flush=True)
    print("=" * 80, flush=True)


def truncate_text(value: str, limit: int = 1200) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [truncated {len(value) - limit} chars]"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================
# SQLite
# ============================================================


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS providers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT NOT NULL UNIQUE,
                base_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_id INTEGER NOT NULL,
                upstream_model TEXT NOT NULL,
                public_model TEXT NOT NULL UNIQUE,
                fetched_at TEXT NOT NULL,
                FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE,
                UNIQUE(provider_id, upstream_model)
            );

            CREATE TABLE IF NOT EXISTS model_preferences (
                provider_id INTEGER NOT NULL,
                upstream_model TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                prefix TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(provider_id, upstream_model),
                FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
            );


            CREATE TABLE IF NOT EXISTS model_priority_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE COLLATE NOCASE,
                prefix TEXT NOT NULL DEFAULT 'A-',
                priority INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def setting_get(key: str) -> str | None:
    conn = db()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,),
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def setting_set(key: str, value: str) -> None:
    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def get_or_create_gateway_key() -> str:
    existing = setting_get("gateway_key")
    if existing:
        return existing

    key = "gw-" + secrets.token_urlsafe(32)
    setting_set("gateway_key", key)
    return key


# ============================================================
# 数据处理
# ============================================================


def mask_key(key: str) -> str:
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * min(12, max(4, len(key) - 8))}{key[-4:]}"


def normalize_identifier(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("标识符不能为空")
    if "/" in value or "\\" in value or any(ch.isspace() for ch in value):
        raise ValueError("标识符不能包含空格、/ 或 \\")
    return value


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value.startswith(("http://", "https://")):
        raise ValueError("URL 必须以 http:// 或 https:// 开头")
    return value


def models_from_response(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        data = payload.get("data")
    else:
        data = None

    if not isinstance(data, list):
        raise ValueError("上游 /models 返回格式不是标准 OpenAI data 列表")

    models: list[str] = []
    for item in data:
        if (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].strip()
        ):
            models.append(item["id"].strip())

    return list(dict.fromkeys(models))


# ============================================================
# 上游 Models 获取
# ============================================================


async def fetch_models(base_url: str, api_key: str) -> list[str]:
    url = base_url.rstrip("/") + "/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    debug_separator("FETCH MODELS")
    debug(f"URL            : {url}")
    debug("Authorization  : Bearer ***")

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
    ) as client:
        response = await client.get(url, headers=headers)

        debug(f"Status         : {response.status_code}")
        debug(f"Final URL      : {response.url}")
        debug(f"Content-Type   : {response.headers.get('content-type', '<none>')}")
        debug(f"Content-Length : {response.headers.get('content-length', '<none>')}")
        debug(f"Body bytes     : {len(response.content)}")

        if DEBUG_PROXY:
            try:
                preview = response.text.replace("\r", "\\r").replace("\n", "\\n")
                debug(f"Body preview   : {truncate_text(preview, 1000)}")
            except Exception as exc:
                debug(f"Body preview unavailable: {exc}")

        response.raise_for_status()
        return models_from_response(response.json())


def save_models(provider_id: int, identifier: str, models: list[str]) -> int:
    conn = db()
    try:
        timestamp = now_iso()
        conn.execute(
            "DELETE FROM models WHERE provider_id=?",
            (provider_id,),
        )

        inserted = 0
        for upstream_model in models:
            public_model = f"{identifier}/{upstream_model}"
            try:
                conn.execute(
                    """
                    INSERT INTO models(
                        provider_id,
                        upstream_model,
                        public_model,
                        fetched_at
                    )
                    VALUES(?,?,?,?)
                    """,
                    (provider_id, upstream_model, public_model, timestamp),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                continue

        conn.commit()
        return inserted
    finally:
        conn.close()


MODEL_REFRESH_TASKS: set[asyncio.Task] = set()
MODEL_REFRESH_LOCK = asyncio.Lock()


def model_public_name(base_public_model: str, prefix: str = "") -> str:
    prefix = str(prefix or "").strip()
    return f"{prefix}{base_public_model}" if prefix else base_public_model


def model_rule_rows() -> list[sqlite3.Row]:
    conn = db()
    try:
        return conn.execute(
            "SELECT id, keyword, prefix, priority, created_at, updated_at "
            "FROM model_priority_rules ORDER BY priority DESC, id ASC"
        ).fetchall()
    finally:
        conn.close()


def model_list_rows() -> list[dict[str, Any]]:
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT
                m.public_model AS base_public_model,
                m.upstream_model,
                m.provider_id,
                p.identifier,
                p.description,
                m.fetched_at
            FROM models m
            JOIN providers p ON p.id=m.provider_id
            ORDER BY m.public_model COLLATE NOCASE
            """
        ).fetchall()
    finally:
        conn.close()

    rules = model_rule_rows()
    result: list[dict[str, Any]] = []
    for row in rows:
        matched_rule = None
        candidates = (str(row["upstream_model"]), str(row["base_public_model"]))
        for rule in rules:
            keyword = str(rule["keyword"]).casefold()
            if any(keyword in name.casefold() for name in candidates):
                matched_rule = rule
                break

        prefix = str(matched_rule["prefix"] or "") if matched_rule else ""
        result.append({
            **dict(row),
            "public_model": model_public_name(row["base_public_model"], prefix),
            "priority": bool(matched_rule and matched_rule["priority"]),
            "prefix": prefix,
            "matched_keyword": str(matched_rule["keyword"]) if matched_rule else "",
        })

    result.sort(key=lambda x: (-int(x["priority"]), str(x["public_model"]).casefold()))
    return result


def model_rules_replace(rules: list[dict[str, Any]]) -> int:
    conn = db()
    try:
        now = now_iso()
        conn.execute("DELETE FROM model_priority_rules")
        for rule in rules:
            conn.execute(
                """
                INSERT INTO model_priority_rules
                    (keyword, prefix, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    rule["keyword"],
                    rule["prefix"],
                    1 if rule.get("priority", True) else 0,
                    now,
                    now,
                ),
            )
        conn.commit()
        return len(rules)
    finally:
        conn.close()


def model_lookup(public_model: str) -> sqlite3.Row | None:
    rows = model_list_rows()
    row = next((r for r in rows if r["public_model"] == public_model), None)
    if row is None:
        row = next((r for r in rows if r["base_public_model"] == public_model), None)
    if row is None:
        return None

    conn = db()
    try:
        return conn.execute(
            """
            SELECT
                m.public_model AS base_public_model,
                m.upstream_model,
                p.id AS provider_id,
                p.identifier,
                p.base_url,
                p.api_key,
                p.description,
                m.fetched_at
            FROM models m
            JOIN providers p ON p.id=m.provider_id
            WHERE m.provider_id=? AND m.upstream_model=?
            LIMIT 1
            """,
            (row["provider_id"], row["upstream_model"]),
        ).fetchone()
    finally:
        conn.close()


def cache_is_stale() -> bool:
    conn = db()
    try:
        rows = conn.execute(
            "SELECT MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest FROM models"
        ).fetchone()
    finally:
        conn.close()
    if not rows or not rows["oldest"]:
        return True
    try:
        oldest = datetime.fromisoformat(rows["oldest"])
        return (datetime.now(timezone.utc) - oldest).total_seconds() >= MODEL_REFRESH_TTL
    except Exception:
        return True


async def refresh_all_models_background(reason: str = "background") -> None:
    async with MODEL_REFRESH_LOCK:
        debug(f"model refresh start: {reason}")
        conn = db()
        try:
            provider_ids = [
                row["id"]
                for row in conn.execute("SELECT id FROM providers ORDER BY id").fetchall()
            ]
        finally:
            conn.close()
        for provider_id in provider_ids:
            try:
                result = await resync_provider(provider_id)
                debug(f"model refresh provider#{provider_id}: {result['model_count']} models")
            except Exception as exc:
                debug(f"model refresh provider#{provider_id} failed: {exc}")
        debug("model refresh complete")


def schedule_model_refresh(reason: str) -> None:
    if any(not task.done() for task in MODEL_REFRESH_TASKS):
        return
    task = asyncio.create_task(refresh_all_models_background(reason))
    MODEL_REFRESH_TASKS.add(task)
    task.add_done_callback(MODEL_REFRESH_TASKS.discard)


# ============================================================
# Provider 数据访问
# ============================================================


def provider_row(provider_id: int) -> sqlite3.Row | None:
    conn = db()
    try:
        return conn.execute(
            "SELECT * FROM providers WHERE id=?",
            (provider_id,),
        ).fetchone()
    finally:
        conn.close()


def provider_list() -> list[dict[str, Any]]:
    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT
                p.id,
                p.identifier,
                p.base_url,
                p.api_key,
                p.description,
                p.created_at,
                p.updated_at,
                COUNT(m.id) AS model_count,
                MAX(m.fetched_at) AS models_fetched_at
            FROM providers p
            LEFT JOIN models m ON m.provider_id=p.id
            GROUP BY p.id
            ORDER BY p.id DESC
            """
        ).fetchall()

        return [
            {
                "id": row["id"],
                "identifier": row["identifier"],
                "base_url": row["base_url"],
                "api_key": mask_key(row["api_key"]),
                "description": row["description"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "model_count": row["model_count"],
                "models_fetched_at": row["models_fetched_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()




# ============================================================
# Pydantic
# ============================================================


class ProviderCreate(BaseModel):
    identifier: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(min_length=1, max_length=1000)
    description: str = Field(default="", max_length=500)


class ProviderUpdate(BaseModel):
    identifier: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=500)


class ModelPriorityRule(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    prefix: str = Field(default="A-", max_length=30)
    priority: bool = True


class ModelPriorityRuleBatch(BaseModel):
    rules: list[ModelPriorityRule] = Field(default_factory=list)


# ============================================================
# Gateway Key 鉴权
# ============================================================


def verify_gateway_key(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer API Key")

    token = authorization[7:].strip()
    gateway_key = setting_get("gateway_key")

    if not gateway_key or not secrets.compare_digest(token, gateway_key):
        raise HTTPException(status_code=401, detail="Gateway API Key 无效")


async def auth(authorization: str | None = Header(default=None)) -> None:
    verify_gateway_key(authorization)


# ============================================================
# Provider 同步
# ============================================================


async def resync_provider(provider_id: int) -> dict[str, Any]:
    row = provider_row(provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="渠道不存在")

    try:
        upstream_models = await fetch_models(
            row["base_url"],
            row["api_key"],
        )
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        raise HTTPException(
            status_code=502,
            detail=(
                f"上游 /models 返回 HTTP {exc.response.status_code}: {body}"
            ),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"获取模型失败: {exc}",
        ) from exc

    count = save_models(
        row["id"],
        row["identifier"],
        upstream_models,
    )

    debug(
        f"provider#{row['id']} "
        f"identifier={row['identifier']} "
        f"synced_models={count}"
    )

    return {
        "provider_id": row["id"],
        "model_count": count,
        "models": [
            f"{row['identifier']}/{m}"
            for m in upstream_models
        ],
    }


# ============================================================
# Lifespan
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    gateway_key = get_or_create_gateway_key()

    print("=" * 80)
    print("Personal OpenAI-Compatible Model Union Gateway")
    print(f"Gateway URL : http://{HOST}:{PORT}")
    print(f"Local URL   : http://127.0.0.1:{PORT}")
    print(f"Gateway Key : {gateway_key}")
    print(f"SQLite      : {DB_PATH}")
    print(f"Proxy Debug : {'ON' if DEBUG_PROXY else 'OFF'}")
    print("注意：控制台不会打印上游 API Key 明文。")
    print("=" * 80)

    conn = db()
    try:
        provider_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM providers ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()

    # 启动不等待上游 /models；先使用 SQLite 中已有模型，后台异步刷新。
    if provider_ids:
        schedule_model_refresh("startup")

    yield


app = FastAPI(
    title="Personal Model Union Gateway",
    version="1.4.0-model-cache",
    lifespan=lifespan,
)


# 浏览器客户端不仅需要 OPTIONS 成功，实际 GET/POST 响应也必须带 CORS 头。
# allow_credentials=False 配合 allow_origins=["*"]，适用于这里的 Bearer Key 认证。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ============================================================
# 前端
# ============================================================

INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Personal Model Union Gateway</title>
<style>
:root {
  font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  color:#202124;
  background:#f5f6f8;
}
* { box-sizing:border-box; }
body { margin:0; }
.container { max-width:1200px; margin:28px auto; padding:0 18px; }
.card {
  background:#fff;
  border:1px solid #e5e7eb;
  border-radius:14px;
  padding:18px;
  margin-bottom:16px;
  box-shadow:0 3px 15px rgba(0,0,0,.04);
}
h1 { font-size:24px; margin:0 0 6px; }
h2 { font-size:17px; margin:0 0 14px; }
.muted { color:#6b7280; font-size:13px; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
label { display:block; font-size:13px; color:#4b5563; margin-bottom:5px; }
input,textarea,button { font:inherit; }
input,textarea {
  width:100%;
  border:1px solid #d1d5db;
  border-radius:9px;
  padding:9px 11px;
  background:#fff;
}
textarea { min-height:76px; resize:vertical; }
.actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
button {
  border:0;
  border-radius:9px;
  padding:9px 13px;
  cursor:pointer;
  background:#111827;
  color:#fff;
}
button.secondary { background:#eef0f3; color:#111827; }
button.danger { background:#b91c1c; }
button.quick { background:#2563eb; }
button.small { padding:6px 9px; font-size:12px; }
table { width:100%; border-collapse:collapse; }
th,td {
  text-align:left;
  padding:10px 8px;
  border-bottom:1px solid #edf0f2;
  vertical-align:top;
  font-size:13px;
}
code {
  background:#f2f4f7;
  padding:2px 5px;
  border-radius:5px;
  word-break:break-all;
}
.status { white-space:pre-wrap; font-size:12px; margin-top:10px; }
.badge {
  display:inline-block;
  background:#eef2ff;
  color:#3730a3;
  border-radius:999px;
  padding:2px 8px;
  font-size:12px;
}
.notice {
  background:#f8fafc;
  border:1px solid #e5e7eb;
  padding:10px 12px;
  border-radius:9px;
  margin-top:10px;
  font-size:12px;
  line-height:1.6;
}
.quick-box {
  margin-top:10px;
  padding:10px 12px;
  background:#eff6ff;
  border:1px solid #bfdbfe;
  border-radius:9px;
  color:#1e3a8a;
  font-size:12px;
}
.provider-actions { display:flex; flex-wrap:wrap; gap:5px; }
.key-local { color:#166534; font-size:11px; }
.model-tools { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:10px; }
.model-tools input[type=text] { max-width:220px; }
.model-check { width:18px !important; height:18px; }
.priority-row { background:#f8fafc; }
@media(max-width:760px) {
  .grid { grid-template-columns:1fr; }
  table { display:block; overflow:auto; }
}
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <h1>Personal Model Union Gateway</h1>
    <div class="muted">
      多个 OpenAI-compatible API 合并为一个入口。每个渠道使用独立标识符，公开模型名不会碰撞。
    </div>
  </div>

  <div class="card">
    <h2>Gateway Key</h2>
    <div class="muted">Gateway Key 保存在浏览器 localStorage 中。</div>
    <div style="margin-top:10px">
      <input id="gatewayKey" type="password" placeholder="粘贴控制台输出的 Gateway Key">
    </div>
    <div class="actions">
      <button onclick="saveGatewayKey()">使用此 Key</button>
      <button class="secondary" onclick="clearGatewayKey()">清除 Gateway Key</button>
      <button class="secondary" onclick="clearLocalKeys()">清除上游 Key</button>
    </div>
    <div id="authStatus" class="status"></div>
  </div>

  <div class="card">
    <h2 id="formTitle">添加渠道</h2>
    <div id="quickInfo" class="quick-box" style="display:none">
      快捷添加已经复制现有渠道信息。通常只需要修改 API Key，并确认标识符。
    </div>

    <div class="grid">
      <div>
        <label>标识符（会成为模型前缀）</label>
        <input id="identifier" placeholder="例如 openai、claude、cheap1">
      </div>

      <div>
        <label>OpenAI-compatible Base URL</label>
        <input id="baseUrl" placeholder="例如 https://example.com/v1">
      </div>

      <div>
        <label>API Key</label>
        <input id="apiKey" type="password" autocomplete="off" placeholder="新增时填写">
        <div id="localKeyHint" class="key-local" style="margin-top:5px"></div>
      </div>

      <div>
        <label>描述</label>
        <input id="description" placeholder="例如 主账号 / 低价备用">
      </div>
    </div>

    <div class="actions">
      <button id="saveBtn" onclick="saveProvider()">添加并获取模型</button>
      <button class="secondary" onclick="resetForm()">清空</button>
    </div>
    <div id="formStatus" class="status"></div>
  </div>

  <div class="card">
    <h2>渠道</h2>
    <div class="notice">
      “快捷添加”会复制现有渠道的 Base URL、描述和本地保存的 API Key，自动生成新的标识符。
    </div>
    <div id="providers" style="margin-top:12px"></div>
  </div>

  <div class="card">
    <h2>优先模型关键词</h2>
    <div class="muted" style="margin-bottom:10px">
      输入核心模型关键词，例如 <code>luna</code>。后端会匹配上游模型名或基础公开模型名中包含该关键词的所有模型，
      自动增加前缀；以后新增的匹配模型也会自动生效。
    </div>
    <div class="grid">
      <div>
        <label>关键词（每行一个，也支持逗号分隔）</label>
        <textarea id="modelKeywords" placeholder="luna
gpt-5
claude"></textarea>
      </div>
      <div>
        <label>自动前缀</label>
        <input id="modelPrefix" type="text" value="A-" placeholder="例如 A-">
        <div class="muted" style="margin-top:6px">一个模型命中多个关键词时，按规则顺序使用第一个命中的规则。</div>
      </div>
    </div>
    <div class="actions">
      <button class="quick" onclick="saveModelRules()">保存规则</button>
      <button class="secondary" onclick="clearModelRules()">清空规则</button>
    </div>
    <div id="ruleStatus" class="status"></div>
    <div id="modelRules" style="margin-top:12px"></div>
  </div>

  <div class="card">
    <h2>模型缓存</h2>
    <div class="muted" style="margin-bottom:10px">
      <code>/v1/models</code> 先返回 SQLite 缓存，随后后台刷新上游；匹配关键词的模型会动态显示前缀别名。
    </div>
    <div id="models"></div>
  </div>
</div>

<script>
const GATEWAY_KEY_STORAGE = 'gatewayGatewayKey';
const PROVIDER_KEYS_STORAGE = 'gatewayProviderKeys';

let gatewayKey = localStorage.getItem(GATEWAY_KEY_STORAGE) || '';
let editingId = null;

const gatewayInput = document.getElementById('gatewayKey');
gatewayInput.value = gatewayKey;

function getProviderKeys() {
  try {
    const value = localStorage.getItem(PROVIDER_KEYS_STORAGE) || '{}';
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function saveProviderKeys(map) {
  localStorage.setItem(PROVIDER_KEYS_STORAGE, JSON.stringify(map));
}

function getLocalProviderKey(providerId) {
  return getProviderKeys()[String(providerId)] || '';
}

function setLocalProviderKey(providerId, key) {
  if (!providerId) return;

  const map = getProviderKeys();

  if (key) {
    map[String(providerId)] = key;
  } else {
    delete map[String(providerId)];
  }

  saveProviderKeys(map);
}

function clearGatewayKey() {
  gatewayKey = '';
  localStorage.removeItem(GATEWAY_KEY_STORAGE);
  gatewayInput.value = '';
  setStatus('authStatus', '已清除 Gateway Key。');
}

function clearLocalKeys() {
  localStorage.removeItem(PROVIDER_KEYS_STORAGE);
  document.getElementById('localKeyHint').textContent = '';
  loadProviders().catch(err => setStatus('authStatus', err.message, true));
  alert('已清除浏览器中保存的全部上游 API Key。');
}

function saveGatewayKey() {
  gatewayKey = gatewayInput.value.trim();
  localStorage.setItem(GATEWAY_KEY_STORAGE, gatewayKey);

  setStatus(
    'authStatus',
    gatewayKey ? '已保存 Gateway Key 到本地。' : 'Gateway Key 为空。',
    !gatewayKey
  );

  loadAll();
}

function headers(json = true) {
  const h = {
    'Authorization': `Bearer ${gatewayKey}`
  };

  if (json) {
    h['Content-Type'] = 'application/json';
  }

  return h;
}

function setStatus(id, msg, error = false) {
  const el = document.getElementById(id);
  el.textContent = msg || '';
  el.style.color = error ? '#b91c1c' : '#166534';
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...headers(options.body !== undefined),
      ...(options.headers || {})
    }
  });

  const text = await response.text();
  let data;

  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }

  if (!response.ok) {
    throw new Error(
      data.detail ||
      data.error?.message ||
      `HTTP ${response.status}`
    );
  }

  return data;
}

async function loadAll() {
  if (!gatewayKey) return;

  try {
    await Promise.all([loadProviders(), loadModels(), loadModelRules()]);
    setStatus('authStatus', 'Gateway Key 有效。');
  } catch (e) {
    setStatus('authStatus', e.message, true);
  }
}

async function loadProviders() {
  const data = await api('/api/providers');
  const box = document.getElementById('providers');

  if (!data.length) {
    box.innerHTML = '<div class="muted">暂无渠道。</div>';
    return;
  }

  box.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>标识符</th>
          <th>URL</th>
          <th>Key</th>
          <th>描述</th>
          <th>模型数</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        ${data.map(p => {
          const localKey = getLocalProviderKey(p.id);
          const providerJson = JSON.stringify(p)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');

          return `
            <tr>
              <td><span class="badge">${esc(p.identifier)}</span></td>
              <td>${esc(p.base_url)}</td>
              <td>
                <code>${esc(p.api_key)}</code>
                ${localKey ? '<div class="key-local">✓ Key 已保存在本地</div>' : ''}
              </td>
              <td>${esc(p.description || '')}</td>
              <td>${p.model_count}</td>
              <td>
                <div class="provider-actions">
                  <button class="quick small" onclick="quickAddProviderFromRow(${providerJson})">快捷添加</button>
                  <button class="secondary small" onclick="editProviderFromRow(${providerJson})">编辑</button>
                  <button class="secondary small" onclick="syncProvider(${p.id})">刷新模型</button>
                  <button class="danger small" onclick="deleteProvider(${p.id})">删除</button>
                </div>
              </td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
  `;
}

async function loadModelRules() {
  const data = await api('/api/models/rules');
  document.getElementById('modelKeywords').value = data.map(r => r.keyword).join('\n');
  if (data[0]?.prefix) document.getElementById('modelPrefix').value = data[0].prefix;

  const box = document.getElementById('modelRules');
  if (!data.length) {
    box.innerHTML = '<div class="muted">暂无优先模型关键词规则。</div>';
    return;
  }
  box.innerHTML = `
    <table>
      <thead><tr><th>关键词</th><th>前缀</th><th>状态</th></tr></thead>
      <tbody>
        ${data.map(r => `
          <tr><td><code>${esc(r.keyword)}</code></td><td><code>${esc(r.prefix)}</code></td><td>${r.priority ? '启用' : '停用'}</td></tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

async function saveModelRules() {
  const raw = document.getElementById('modelKeywords').value;
  const prefix = document.getElementById('modelPrefix').value.trim();
  if (!prefix) {
    setStatus('ruleStatus', '前缀不能为空。', true);
    return;
  }
  if (/\s/.test(prefix) || prefix.includes('/') || prefix.includes('\\')) {
    setStatus('ruleStatus', '前缀不能包含空格、/ 或 \\。', true);
    return;
  }

  const unique = [];
  const seen = new Set();
  for (const keyword of raw.split(/[\n,，]+/).map(v => v.trim()).filter(Boolean)) {
    const key = keyword.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      unique.push(keyword);
    }
  }
  if (!unique.length) {
    setStatus('ruleStatus', '至少填写一个模型关键词。', true);
    return;
  }

  try {
    const result = await api('/api/models/rules', {
      method: 'POST',
      body: JSON.stringify({
        rules: unique.map(keyword => ({keyword, prefix, priority:true}))
      })
    });
    setStatus('ruleStatus', `已保存 ${result.updated} 条规则。`);
    await Promise.all([loadModelRules(), loadModels()]);
  } catch (e) {
    setStatus('ruleStatus', e.message, true);
  }
}

async function clearModelRules() {
  if (!confirm('确定清空全部优先模型关键词规则吗？')) return;
  try {
    await api('/api/models/rules', {
      method:'POST',
      body:JSON.stringify({rules:[]})
    });
    await Promise.all([loadModelRules(), loadModels()]);
    setStatus('ruleStatus', '已清空优先模型规则。');
  } catch (e) {
    setStatus('ruleStatus', e.message, true);
  }
}

async function loadModels() {
  const data = await api('/api/models');
  const box = document.getElementById('models');
  if (!data.length) {
    box.innerHTML = '<div class="muted">暂无模型。添加渠道后会自动获取。</div>';
    return;
  }
  box.innerHTML = `
    <table>
      <thead><tr><th>公开模型</th><th>基础模型</th><th>上游模型</th><th>渠道</th><th>优先</th><th>命中关键词</th></tr></thead>
      <tbody>
        ${data.map(m => `
          <tr class="${m.priority ? 'priority-row' : ''}">
            <td><code>${esc(m.public_model)}</code></td>
            <td><code>${esc(m.base_public_model)}</code></td>
            <td>${esc(m.upstream_model)}</td>
            <td>${esc(m.identifier)}</td>
            <td>${m.priority ? '✓' : ''}</td>
            <td>${esc(m.matched_keyword || '')}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}


async function saveProvider() {
  const enteredKey = document.getElementById('apiKey').value.trim();
  const payload = {
    identifier: document.getElementById('identifier').value.trim(),
    base_url: document.getElementById('baseUrl').value.trim(),
    api_key: enteredKey,
    description: document.getElementById('description').value.trim()
  };

  if (!payload.identifier || !payload.base_url || (!editingId && !payload.api_key)) {
    setStatus(
      'formStatus',
      '新增时标识符、URL、Key 都必须填写。',
      true
    );
    return;
  }

  try {
    const data = await api(
      editingId ? `/api/providers/${editingId}` : '/api/providers',
      {
        method: editingId ? 'PUT' : 'POST',
        body: JSON.stringify(payload)
      }
    );

    if (payload.api_key) {
      setLocalProviderKey(data.id, payload.api_key);
    }

    setStatus(
      'formStatus',
      `已保存：${data.identifier}，Key 显示为 ${data.api_key}`
    );

    resetForm();
    await Promise.all([loadProviders(), loadModels(), loadModelRules()]);
  } catch (e) {
    setStatus('formStatus', e.message, true);
  }
}

function generateQuickIdentifier(identifier) {
  const base = identifier
    .replace(/[^a-zA-Z0-9_-]/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '') || 'provider';

  const existing = Array.from(
    document.querySelectorAll('#providers .badge')
  ).map(el => el.textContent.trim());

  let candidate = `${base}-copy`;
  let n = 2;

  while (existing.includes(candidate)) {
    candidate = `${base}-copy${n}`;
    n += 1;
  }

  return candidate;
}

function quickAddProviderFromRow(p) {
  quickAddProvider(p);
}

function editProviderFromRow(p) {
  editProvider(p);
}

function quickAddProvider(p) {
  editingId = null;

  document.getElementById('formTitle').textContent = '快捷添加渠道';
  document.getElementById('saveBtn').textContent = '添加并获取模型';
  document.getElementById('quickInfo').style.display = 'block';

  document.getElementById('identifier').value = generateQuickIdentifier(p.identifier);
  document.getElementById('baseUrl').value = p.base_url;

  const localKey = getLocalProviderKey(p.id);
  document.getElementById('apiKey').value = localKey;
  document.getElementById('apiKey').placeholder = localKey
    ? '已自动填入本地保存的 Key，可直接替换'
    : '该渠道没有本地 Key，请填写新的 Key';

  document.getElementById('description').value = p.description || '';

  document.getElementById('localKeyHint').textContent = localKey
    ? '✓ 已从本地保存的该渠道 Key 自动填充'
    : '该渠道没有保存在本地的真实 Key';

  setStatus(
    'formStatus',
    `已基于 ${p.identifier} 创建快捷添加草稿。`
  );

  window.scrollTo({ top: 0, behavior: 'smooth' });

  setTimeout(() => {
    const input = document.getElementById('apiKey');
    input.focus();
    input.select();
  }, 250);
}

function editProvider(p) {
  editingId = p.id;

  document.getElementById('formTitle').textContent = '编辑渠道';
  document.getElementById('saveBtn').textContent = '保存并刷新模型';
  document.getElementById('quickInfo').style.display = 'none';

  document.getElementById('identifier').value = p.identifier;
  document.getElementById('baseUrl').value = p.base_url;

  const localKey = getLocalProviderKey(p.id);
  document.getElementById('apiKey').value = localKey;
  document.getElementById('apiKey').placeholder = localKey
    ? `本地已保存：${p.api_key}；修改后可直接替换`
    : `当前：${p.api_key}；浏览器未保存真实 Key`;

  document.getElementById('description').value = p.description || '';

  document.getElementById('localKeyHint').textContent = localKey
    ? '✓ API Key 已从浏览器 localStorage 恢复'
    : '浏览器没有保存该渠道的真实 API Key';

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function resetForm() {
  editingId = null;

  document.getElementById('formTitle').textContent = '添加渠道';
  document.getElementById('saveBtn').textContent = '添加并获取模型';
  document.getElementById('quickInfo').style.display = 'none';

  ['identifier', 'baseUrl', 'apiKey', 'description'].forEach(id => {
    document.getElementById(id).value = '';
  });

  document.getElementById('apiKey').placeholder = '新增时填写';
  document.getElementById('localKeyHint').textContent = '';
}

async function syncProvider(id) {
  try {
    const result = await api(`/api/providers/${id}/sync`, {
      method: 'POST'
    });

    setStatus(
      'formStatus',
      `已刷新 ${result.model_count} 个模型。`
    );

    await Promise.all([loadProviders(), loadModels()]);
  } catch (e) {
    setStatus('formStatus', e.message, true);
  }
}

async function deleteProvider(id) {
  if (!confirm('确定删除这个渠道以及它的模型列表吗？')) {
    return;
  }

  try {
    await api(`/api/providers/${id}`, {
      method: 'DELETE'
    });

    setLocalProviderKey(id, '');
    await Promise.all([loadProviders(), loadModels()]);
  } catch (e) {
    setStatus('formStatus', e.message, true);
  }
}

function esc(value) {
  return String(value ?? '').replace(
    /[&<>'"]/g,
    c => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;'
    })[c]
  );
}

loadAll();
</script>
</body>
</html>
'''


# ============================================================
# 首页
# ============================================================


@app.get("/", include_in_schema=False)
async def index() -> Response:
    return Response(
        content=INDEX_HTML,
        media_type="text/html; charset=utf-8",
    )


# ============================================================
# Provider API
# ============================================================


@app.get("/api/providers", dependencies=[Depends(auth)])
async def api_provider_list() -> list[dict[str, Any]]:
    return provider_list()


@app.post("/api/providers", dependencies=[Depends(auth)])
async def api_provider_create(data: ProviderCreate) -> dict[str, Any]:
    try:
        identifier = normalize_identifier(data.identifier)
        base_url = normalize_base_url(data.base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    api_key = data.api_key.strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key 不能为空")

    conn = db()
    try:
        timestamp = now_iso()
        cursor = conn.execute(
            """
            INSERT INTO providers(
                identifier,
                base_url,
                api_key,
                description,
                created_at,
                updated_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                identifier,
                base_url,
                api_key,
                data.description.strip(),
                timestamp,
                timestamp,
            ),
        )
        provider_id = int(cursor.lastrowid)
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="identifier 已存在") from exc
    finally:
        conn.close()

    try:
        await resync_provider(provider_id)
    except HTTPException as exc:
        print(
            f"[create] provider#{provider_id} model sync failed: {exc.detail}",
            flush=True,
        )

    row = provider_row(provider_id)
    assert row is not None

    return {
        "id": row["id"],
        "identifier": row["identifier"],
        "base_url": row["base_url"],
        "api_key": mask_key(row["api_key"]),
        "description": row["description"],
    }


@app.put("/api/providers/{provider_id}", dependencies=[Depends(auth)])
async def api_provider_update(
    provider_id: int,
    data: ProviderUpdate,
) -> dict[str, Any]:
    row = provider_row(provider_id)
    if not row:
        raise HTTPException(status_code=404, detail="渠道不存在")

    identifier = row["identifier"]
    base_url = row["base_url"]
    api_key = row["api_key"]
    description = row["description"]

    if data.identifier is not None:
        try:
            identifier = normalize_identifier(data.identifier)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if data.base_url is not None:
        try:
            base_url = normalize_base_url(data.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if data.api_key:
        new_key = data.api_key.strip()
        if new_key and new_key != row["api_key"] and set(new_key) != {"*"}:
            api_key = new_key

    if data.description is not None:
        description = data.description.strip()

    conn = db()
    try:
        conn.execute(
            """
            UPDATE providers
            SET
                identifier=?,
                base_url=?,
                api_key=?,
                description=?,
                updated_at=?
            WHERE id=?
            """,
            (
                identifier,
                base_url,
                api_key,
                description,
                now_iso(),
                provider_id,
            ),
        )
        conn.execute(
            "DELETE FROM models WHERE provider_id=?",
            (provider_id,),
        )
        conn.execute(
            "DELETE FROM model_preferences WHERE provider_id=?",
            (provider_id,),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="identifier 已存在") from exc
    finally:
        conn.close()

    try:
        await resync_provider(provider_id)
    except HTTPException as exc:
        print(
            f"[update] provider#{provider_id} model sync failed: {exc.detail}",
            flush=True,
        )

    row = provider_row(provider_id)
    assert row is not None

    return {
        "id": row["id"],
        "identifier": row["identifier"],
        "base_url": row["base_url"],
        "api_key": mask_key(row["api_key"]),
        "description": row["description"],
    }


@app.delete("/api/providers/{provider_id}", dependencies=[Depends(auth)])
async def api_provider_delete(provider_id: int) -> dict[str, Any]:
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM providers WHERE id=?",
            (provider_id,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="渠道不存在")

        conn.execute(
            "DELETE FROM models WHERE provider_id=?",
            (provider_id,),
        )
        conn.execute(
            "DELETE FROM providers WHERE id=?",
            (provider_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True}


@app.post("/api/providers/{provider_id}/sync", dependencies=[Depends(auth)])
async def api_provider_sync(provider_id: int) -> dict[str, Any]:
    return await resync_provider(provider_id)


# ============================================================
# Models API
# ============================================================


@app.get("/api/models", dependencies=[Depends(auth)])
async def api_internal_models() -> list[dict[str, Any]]:
    return model_list_rows()


@app.get("/api/models/rules", dependencies=[Depends(auth)])
async def api_model_rules() -> list[dict[str, Any]]:
    return [dict(row) for row in model_rule_rows()]


@app.post("/api/models/rules", dependencies=[Depends(auth)])
async def api_model_rules_save(data: ModelPriorityRuleBatch) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for rule in data.rules:
        keyword = rule.keyword.strip()
        prefix = rule.prefix.strip()
        if not keyword:
            continue
        if any(ch.isspace() for ch in keyword):
            raise HTTPException(status_code=400, detail=f"关键词不能包含空格: {keyword}")
        if not prefix:
            raise HTTPException(status_code=400, detail="模型前缀不能为空")
        if any(ch.isspace() or ch in "/\\" for ch in prefix):
            raise HTTPException(status_code=400, detail="模型前缀不能包含空格、/ 或 \\。")

        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "keyword": keyword,
            "prefix": prefix,
            "priority": rule.priority,
        })

    count = model_rules_replace(normalized)
    return {
        "ok": True,
        "updated": count,
        "rules": [dict(row) for row in model_rule_rows()],
    }


@app.get("/v1/models")
async def openai_models() -> dict[str, Any]:
    # 核心缓存路径：先读 SQLite，立即返回；后台再向真实上游刷新。
    rows = model_list_rows()
    if cache_is_stale():
        schedule_model_refresh("/v1/models")

    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "object": "list",
        "data": [
            {
                "id": row["public_model"],
                "object": "model",
                "created": (
                    int(datetime.fromisoformat(row["fetched_at"]).timestamp())
                    if row["fetched_at"]
                    else now
                ),
                "owned_by": "personal-gateway",
                "permission": [],
            }
            for row in rows
        ],
    }




# ============================================================
# 透明代理
# ============================================================

HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


REQUEST_DROP_HEADERS = HOP_BY_HOP | {
    "authorization",
}


RESPONSE_DROP_HEADERS = HOP_BY_HOP | {
    "content-length",
}


def outgoing_headers(
    request: Request,
    upstream_key: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}

    for name, value in request.headers.items():
        lname = name.lower()
        if lname in REQUEST_DROP_HEADERS:
            continue
        headers[name] = value

    headers["Authorization"] = f"Bearer {upstream_key}"
    return headers


def response_headers(headers: httpx.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in RESPONSE_DROP_HEADERS:
            continue
        result[name] = value
    return result


@app.api_route(
    "/v1/{path:path}",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
    ],
    dependencies=[Depends(auth)],
)
async def transparent_v1_proxy(
    request: Request,
    path: str,
) -> Response:
    """
    极简透明代理：
    1. 只解析 JSON body 中的 model 用于路由。
    2. 只修改 model 为上游模型名。
    3. Authorization 替换为上游 Key。
    4. 其他请求字段尽量原样转发。
    5. 上游响应状态码、响应头、响应体流直接返回。
    """

    query_string = request.url.query
    incoming_url = str(request.url)

    debug_separator(
        f"CLIENT REQUEST {request.method} {request.url.path}"
    )
    debug(f"Incoming URL  : {incoming_url}")
    debug(f"Query string  : {query_string or '<none>'}")

    body = await request.body()
    debug(f"Incoming bytes: {len(body)}")

    if not body:
        raise HTTPException(
            status_code=400,
            detail="请求体为空，无法从 body 中获取 model",
        )

    content_type = request.headers.get("content-type", "")
    debug(f"Content-Type  : {content_type or '<none>'}")

    if "application/json" not in content_type.lower():
        raise HTTPException(
            status_code=400,
            detail="Gateway 当前要求 application/json 请求体",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"请求体不是有效 JSON: {exc}",
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="请求 JSON 必须是对象",
        )

    public_model = payload.get("model")
    if not isinstance(public_model, str) or not public_model.strip():
        raise HTTPException(
            status_code=400,
            detail="缺少 model",
        )

    public_model = public_model.strip()
    route = model_lookup(public_model)

    debug(f"Public model  : {public_model}")

    if not route:
        debug(f"Unknown model : {public_model}")
        raise HTTPException(
            status_code=404,
            detail=f"未知模型: {public_model}",
        )

    upstream_model = route["upstream_model"]
    base_url = route["base_url"].rstrip("/")

    payload["model"] = upstream_model

    # 保留原有 JSON 结构，仅重新序列化以替换 model。
    body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    # FastAPI 路由 /v1/{path:path} 会把前面的 /v1 去掉，
    # 因此 path 实际上是 "chat/completions"。
    # 上游 OpenAI-compatible API 需要完整的 /v1/chat/completions。
    #
    # Base URL 约定填写 API 根地址，例如：
    #   https://api.voltapi.ai
    # 最终请求：
    #   https://api.voltapi.ai/v1/chat/completions
    #
    # 为兼容已经填写 /v1 的旧配置，这里避免重复 /v1。
    clean_path = path.lstrip("/")

    if clean_path == "v1" or clean_path.startswith("v1/"):
        request_path = clean_path
    else:
        request_path = f"v1/{clean_path}"

    if base_url.endswith("/v1"):
        # Base URL 已经包含 /v1，只拼接其后的具体 endpoint。
        upstream_url = f"{base_url}/{clean_path}"
    else:
        upstream_url = f"{base_url}/{request_path}"

    if query_string:
        upstream_url += f"?{query_string}"

    headers = outgoing_headers(
        request,
        route["api_key"],
    )

    debug_separator("UPSTREAM REQUEST")
    debug(f"Provider      : {route['identifier']}")
    debug(f"Public model  : {public_model}")
    debug(f"Upstream model: {upstream_model}")
    debug(f"Method        : {request.method}")
    debug(f"URL           : {upstream_url}")
    debug(f"Request bytes : {len(body)}")
    debug("Authorization : Bearer ***")

    client = httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
    )

    try:
        upstream_request = client.build_request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body,
        )

        upstream_response = await client.send(
            upstream_request,
            stream=True,
        )

    except httpx.RequestError as exc:
        await client.aclose()
        debug_separator("UPSTREAM CONNECTION ERROR")
        debug(f"{type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=502,
            detail=f"连接上游失败: {exc}",
        ) from exc

    debug_separator("UPSTREAM RESPONSE")
    debug(f"Status          : {upstream_response.status_code}")
    debug(f"URL             : {upstream_response.url}")
    debug(f"Content-Type    : {upstream_response.headers.get('content-type', '<none>')}")
    debug(f"Content-Length  : {upstream_response.headers.get('content-length', '<none>')}")
    debug(f"Content-Encoding: {upstream_response.headers.get('content-encoding', '<none>')}")
    debug(f"Location        : {upstream_response.headers.get('location', '<none>')}")

    if upstream_response.status_code in {301, 302, 303, 307, 308}:
        debug("Redirect received; follow_redirects=False, returning redirect to client.")

    # 错误响应也不改写正文：读取出来后原样返回，便于定位上游问题。
    if upstream_response.status_code >= 400:
        try:
            error_body = await upstream_response.aread()
            debug(f"Error body bytes: {len(error_body)}")

            if DEBUG_PROXY:
                preview = error_body.decode(
                    "utf-8",
                    errors="replace",
                ).replace("\r", "\\r").replace("\n", "\\n")
                debug(f"Error preview  : {truncate_text(preview, 2000)}")

            headers_out = response_headers(upstream_response.headers)

            return Response(
                content=error_body,
                status_code=upstream_response.status_code,
                headers=headers_out,
                media_type=None,
            )
        finally:
            await upstream_response.aclose()
            await client.aclose()

    headers_out = response_headers(upstream_response.headers)

    async def body_iterator() -> AsyncIterator[bytes]:
        total_bytes = 0
        chunk_count = 0
        first_chunk = True

        try:
            # raw：不主动解析/重编码上游响应体。
            async for chunk in upstream_response.aiter_raw():
                chunk_count += 1
                total_bytes += len(chunk)

                if first_chunk:
                    first_chunk = False
                    debug_separator("FIRST UPSTREAM BODY CHUNK")
                    debug(f"First chunk bytes: {len(chunk)}")

                    if len(chunk) > 0:
                        content_type = upstream_response.headers.get(
                            "content-type",
                            "",
                        ).lower()

                        if (
                            "text" in content_type
                            or "json" in content_type
                            or "event-stream" in content_type
                        ):
                            preview = chunk.decode(
                                "utf-8",
                                errors="replace",
                            ).replace("\r", "\\r").replace("\n", "\\n")
                            debug(
                                "First chunk preview: "
                                + truncate_text(preview, 2000)
                            )
                        else:
                            debug("First chunk appears binary/non-text.")

                yield chunk

        except Exception as exc:
            debug_separator("UPSTREAM STREAM ERROR")
            debug(f"{type(exc).__name__}: {exc}")
            raise

        finally:
            debug_separator("UPSTREAM STREAM COMPLETE")
            debug(f"HTTP status : {upstream_response.status_code}")
            debug(f"Chunks      : {chunk_count}")
            debug(f"Total bytes : {total_bytes}")

            if total_bytes == 0:
                debug("WARNING: upstream returned zero response bytes.")

            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iterator(),
        status_code=upstream_response.status_code,
        headers=headers_out,
        media_type=None,
    )


# ============================================================
# RuntimeError
# ============================================================


@app.exception_handler(RuntimeError)
async def runtime_error_handler(
    _: Request,
    exc: RuntimeError,
) -> JSONResponse:
    debug(f"RuntimeError: {exc}")
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "message": str(exc),
                "type": "upstream_error",
            }
        },
    )


# ============================================================
# Main
# ============================================================


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
    )
