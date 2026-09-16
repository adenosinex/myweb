from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse


# ============================================================
# 独立日志旁路代理
# ============================================================
#
# 原 Gateway：127.0.0.1:4000
# 本服务：    0.0.0.0:4001
#
# 客户端：
#   http://127.0.0.1:4001/v1
#
# 日志：
#   gateway_logs.db
#
# 额外监控：
#   - 客户端请求体大小
#   - Prompt Token
#   - Cached Token
#   - 未缓存 Token
#   - 缓存比例
#   - Prompt / 请求体放大倍数
#   - system / developer / user / assistant / tool
#   - tools 数量及 schema 大小
#   - payload SHA256（只保存哈希，不保存正文）
#   - 异常评分
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
CORE_BASE_URL = os.getenv(
    "CORE_BASE_URL",
    "http://127.0.0.1:4000",
).rstrip("/")

HOST = os.getenv("LOG_HOST", "0.0.0.0")
PORT = int(os.getenv("LOG_PORT", "4001"))
LOG_DB_PATH = Path(os.getenv("LOG_DB", BASE_DIR / "gateway_logs.db"))
TIMEOUT = float(os.getenv("LOG_PROXY_TIMEOUT", "300"))
LOG_ADMIN_KEY = os.getenv("LOG_ADMIN_KEY", "")


# ============================================================
# 时间 / SQLite
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(LOG_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    conn = db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_uuid TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                public_model TEXT,
                stream INTEGER NOT NULL DEFAULT 0,
                status_code INTEGER,
                request_bytes INTEGER NOT NULL DEFAULT 0,
                response_bytes INTEGER NOT NULL DEFAULT 0,
                client_body_bytes INTEGER NOT NULL DEFAULT 0,
                forwarded_body_bytes INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                reasoning_tokens INTEGER,
                cached_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_creation_tokens INTEGER,
                uncached_prompt_tokens INTEGER,
                cache_ratio REAL,
                token_byte_ratio REAL,
                estimated_client_tokens INTEGER,
                payload_messages INTEGER,
                system_messages INTEGER,
                developer_messages INTEGER,
                user_messages INTEGER,
                assistant_messages INTEGER,
                tool_messages INTEGER,
                tools_count INTEGER,
                tool_schema_bytes INTEGER,
                system_prompt_chars INTEGER,
                developer_prompt_chars INTEGER,
                message_content_chars INTEGER,
                payload_sha256 TEXT,
                suspicion_score INTEGER,
                suspicion_level TEXT,
                suspicion_flags TEXT,
                ttft_ms REAL,
                duration_ms REAL,
                output_tokens_per_sec REAL,
                input_tokens_per_sec REAL,
                error TEXT,
                client_ip TEXT,
                user_agent TEXT,
                finish_reason TEXT,
                raw_usage_json TEXT,
                raw_error_json TEXT,
                payload_structure_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_request_logs_created_at
                ON request_logs(created_at);

            CREATE INDEX IF NOT EXISTS idx_request_logs_model
                ON request_logs(public_model);

            CREATE INDEX IF NOT EXISTS idx_request_logs_status
                ON request_logs(status_code);

            CREATE INDEX IF NOT EXISTS idx_request_logs_cache_read
                ON request_logs(cache_read_tokens);

            CREATE INDEX IF NOT EXISTS idx_request_logs_suspicion
                ON request_logs(suspicion_level);

            CREATE INDEX IF NOT EXISTS idx_request_logs_payload_sha
                ON request_logs(payload_sha256);
            """
        )

        existing = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(request_logs)"
            ).fetchall()
        }

        migrations = {
            "client_body_bytes": "ALTER TABLE request_logs ADD COLUMN client_body_bytes INTEGER",
            "forwarded_body_bytes": "ALTER TABLE request_logs ADD COLUMN forwarded_body_bytes INTEGER",
            "uncached_prompt_tokens": "ALTER TABLE request_logs ADD COLUMN uncached_prompt_tokens INTEGER",
            "cache_ratio": "ALTER TABLE request_logs ADD COLUMN cache_ratio REAL",
            "token_byte_ratio": "ALTER TABLE request_logs ADD COLUMN token_byte_ratio REAL",
            "estimated_client_tokens": "ALTER TABLE request_logs ADD COLUMN estimated_client_tokens INTEGER",
            "payload_messages": "ALTER TABLE request_logs ADD COLUMN payload_messages INTEGER",
            "system_messages": "ALTER TABLE request_logs ADD COLUMN system_messages INTEGER",
            "developer_messages": "ALTER TABLE request_logs ADD COLUMN developer_messages INTEGER",
            "user_messages": "ALTER TABLE request_logs ADD COLUMN user_messages INTEGER",
            "assistant_messages": "ALTER TABLE request_logs ADD COLUMN assistant_messages INTEGER",
            "tool_messages": "ALTER TABLE request_logs ADD COLUMN tool_messages INTEGER",
            "tools_count": "ALTER TABLE request_logs ADD COLUMN tools_count INTEGER",
            "tool_schema_bytes": "ALTER TABLE request_logs ADD COLUMN tool_schema_bytes INTEGER",
            "system_prompt_chars": "ALTER TABLE request_logs ADD COLUMN system_prompt_chars INTEGER",
            "developer_prompt_chars": "ALTER TABLE request_logs ADD COLUMN developer_prompt_chars INTEGER",
            "message_content_chars": "ALTER TABLE request_logs ADD COLUMN message_content_chars INTEGER",
            "payload_sha256": "ALTER TABLE request_logs ADD COLUMN payload_sha256 TEXT",
            "suspicion_score": "ALTER TABLE request_logs ADD COLUMN suspicion_score INTEGER",
            "suspicion_level": "ALTER TABLE request_logs ADD COLUMN suspicion_level TEXT",
            "suspicion_flags": "ALTER TABLE request_logs ADD COLUMN suspicion_flags TEXT",
            "payload_structure_json": "ALTER TABLE request_logs ADD COLUMN payload_structure_json TEXT",
        }

        for column, sql in migrations.items():
            if column not in existing:
                conn.execute(sql)

        conn.commit()
    finally:
        conn.close()


def insert_log(row: dict[str, Any]) -> None:
    conn = db()
    try:
        conn.execute(
            """
            INSERT INTO request_logs (
                request_uuid, created_at, started_at, finished_at,
                method, path, public_model, stream, status_code,
                request_bytes, response_bytes,
                client_body_bytes, forwarded_body_bytes,
                prompt_tokens, completion_tokens, total_tokens, reasoning_tokens,
                cached_tokens, cache_read_tokens, cache_creation_tokens,
                uncached_prompt_tokens, cache_ratio, token_byte_ratio,
                estimated_client_tokens,
                payload_messages, system_messages, developer_messages,
                user_messages, assistant_messages, tool_messages,
                tools_count, tool_schema_bytes,
                system_prompt_chars, developer_prompt_chars,
                message_content_chars, payload_sha256,
                suspicion_score, suspicion_level, suspicion_flags,
                ttft_ms, duration_ms,
                output_tokens_per_sec, input_tokens_per_sec,
                error, client_ip, user_agent, finish_reason,
                raw_usage_json, raw_error_json, payload_structure_json
            ) VALUES (
                :request_uuid, :created_at, :started_at, :finished_at,
                :method, :path, :public_model, :stream, :status_code,
                :request_bytes, :response_bytes,
                :client_body_bytes, :forwarded_body_bytes,
                :prompt_tokens, :completion_tokens, :total_tokens, :reasoning_tokens,
                :cached_tokens, :cache_read_tokens, :cache_creation_tokens,
                :uncached_prompt_tokens, :cache_ratio, :token_byte_ratio,
                :estimated_client_tokens,
                :payload_messages, :system_messages, :developer_messages,
                :user_messages, :assistant_messages, :tool_messages,
                :tools_count, :tool_schema_bytes,
                :system_prompt_chars, :developer_prompt_chars,
                :message_content_chars, :payload_sha256,
                :suspicion_score, :suspicion_level, :suspicion_flags,
                :ttft_ms, :duration_ms,
                :output_tokens_per_sec, :input_tokens_per_sec,
                :error, :client_ip, :user_agent, :finish_reason,
                :raw_usage_json, :raw_error_json, :payload_structure_json
            )
            """,
            row,
        )
        conn.commit()
    finally:
        conn.close()


def delete_logs_before(cutoff_iso: str) -> int:
    conn = db()
    try:
        cur = conn.execute(
            "DELETE FROM request_logs WHERE created_at < ?",
            (cutoff_iso,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ============================================================
# Admin 鉴权
# ============================================================

def check_admin(request: Request) -> None:
    if not LOG_ADMIN_KEY:
        return

    supplied = request.headers.get("X-Log-Admin-Key", "")
    if not supplied or not secrets.compare_digest(
        supplied,
        LOG_ADMIN_KEY,
    ):
        raise HTTPException(
            status_code=401,
            detail="Log Admin Key 无效",
        )


# ============================================================
# 基础工具
# ============================================================

def safe_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def json_compact_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except Exception:
        return 0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ============================================================
# Payload 结构分析
# ============================================================

def recursive_text_length(value: Any) -> int:
    if value is None:
        return 0

    if isinstance(value, str):
        return len(value)

    if isinstance(value, list):
        return sum(recursive_text_length(x) for x in value)

    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key in {
                "text",
                "content",
                "input",
                "output_text",
                "instructions",
            }:
                total += recursive_text_length(item)
        return total

    return 0


def analyze_message_list(messages: Any) -> dict[str, Any]:
    result = {
        "payload_messages": 0,
        "system_messages": 0,
        "developer_messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_messages": 0,
        "system_prompt_chars": 0,
        "developer_prompt_chars": 0,
        "message_content_chars": 0,
    }

    if not isinstance(messages, list):
        return result

    for message in messages:
        if not isinstance(message, dict):
            continue

        result["payload_messages"] += 1

        role = str(message.get("role") or "").lower()
        content = message.get("content")
        content_length = recursive_text_length(content)

        result["message_content_chars"] += content_length

        if role == "system":
            result["system_messages"] += 1
            result["system_prompt_chars"] += content_length
        elif role == "developer":
            result["developer_messages"] += 1
            result["developer_prompt_chars"] += content_length
        elif role == "user":
            result["user_messages"] += 1
        elif role == "assistant":
            result["assistant_messages"] += 1
        elif role == "tool":
            result["tool_messages"] += 1

    return result


def analyze_payload(
    payload: dict[str, Any] | None,
    original_body: bytes,
) -> dict[str, Any]:
    result = {
        "payload_messages": 0,
        "system_messages": 0,
        "developer_messages": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_messages": 0,
        "tools_count": 0,
        "tool_schema_bytes": 0,
        "system_prompt_chars": 0,
        "developer_prompt_chars": 0,
        "message_content_chars": 0,
        "payload_sha256": sha256_bytes(original_body),
        "estimated_client_tokens": (
            max(1, round(len(original_body) / 4))
            if original_body
            else 0
        ),
    }

    if not payload:
        return result

    # Chat Completions
    message_info = analyze_message_list(
        payload.get("messages")
    )

    for key, value in message_info.items():
        result[key] = value

    # Responses API
    instructions = payload.get("instructions")
    if instructions not in (None, ""):
        instructions_chars = recursive_text_length(
            instructions
        )
        result["system_prompt_chars"] += instructions_chars
        result["message_content_chars"] += instructions_chars
        if instructions_chars > 0:
            result["system_messages"] += 1

    input_value = payload.get("input")

    if isinstance(input_value, list):
        input_info = analyze_message_list(input_value)
        for key, value in input_info.items():
            result[key] += value
    elif isinstance(input_value, str):
        result["payload_messages"] += 1
        result["user_messages"] += 1
        result["message_content_chars"] += len(input_value)

    tools = payload.get("tools")
    if isinstance(tools, list):
        result["tools_count"] = len(tools)
        result["tool_schema_bytes"] = json_compact_size(tools)

    functions = payload.get("functions")
    if isinstance(functions, list):
        result["tools_count"] += len(functions)
        result["tool_schema_bytes"] += json_compact_size(functions)

    return result


# ============================================================
# 异常检测
# ============================================================

def analyze_suspicion(
    prompt_tokens: int | None,
    cached_tokens: int | None,
    request_bytes: int,
    payload_info: dict[str, Any],
) -> dict[str, Any]:
    prompt = max(int(prompt_tokens or 0), 0)
    cached = max(int(cached_tokens or 0), 0)

    if prompt > 0:
        cached = min(cached, prompt)

    uncached = max(prompt - cached, 0)

    cache_ratio = cached / prompt if prompt > 0 else None
    token_byte_ratio = (
        prompt / request_bytes
        if prompt > 0 and request_bytes > 0
        else None
    )

    estimated_client_tokens = payload_info.get(
        "estimated_client_tokens"
    )

    score = 0
    flags: list[str] = []

    # 大 Prompt + 小请求体
    if prompt >= 3000 and request_bytes <= 2000:
        score += 25
        flags.append("large_prompt_vs_small_request")

    if prompt >= 5000 and request_bytes <= 1000:
        score += 20
        flags.append("very_large_prompt_vs_tiny_request")

    # Prompt / 客户端请求体
    if token_byte_ratio is not None:
        if token_byte_ratio >= 10:
            score += 15
            flags.append("high_token_byte_ratio")

        if token_byte_ratio >= 20:
            score += 15
            flags.append("very_high_token_byte_ratio")

        if token_byte_ratio >= 30:
            score += 10
            flags.append("extreme_token_byte_ratio")

    # 大 Prompt + 高缓存
    if (
        prompt >= 3000
        and cache_ratio is not None
        and cache_ratio >= 0.70
    ):
        score += 20
        flags.append("large_prompt_high_cache_ratio")

    if (
        prompt >= 5000
        and cache_ratio is not None
        and cache_ratio >= 0.80
    ):
        score += 15
        flags.append("very_large_prompt_very_high_cache")

    # 粗略客户端 Token 与实际 Prompt 的差距
    if estimated_client_tokens and prompt > 0:
        expansion_ratio = prompt / estimated_client_tokens

        if expansion_ratio >= 5:
            score += 10
            flags.append("prompt_expansion_x5")

        if expansion_ratio >= 10:
            score += 10
            flags.append("prompt_expansion_x10")

    # 看不见的超大上下文
    visible_messages = int(
        payload_info.get("payload_messages") or 0
    )
    system_messages = int(
        payload_info.get("system_messages") or 0
    )
    developer_messages = int(
        payload_info.get("developer_messages") or 0
    )
    tools_count = int(
        payload_info.get("tools_count") or 0
    )
    message_chars = int(
        payload_info.get("message_content_chars") or 0
    )

    if (
        prompt >= 3000
        and visible_messages <= 2
        and message_chars <= 1000
        and system_messages == 0
        and developer_messages == 0
        and tools_count == 0
    ):
        score += 20
        flags.append("large_prompt_without_visible_context")

    flags = list(dict.fromkeys(flags))

    if score >= 60:
        level = "HIGH"
    elif score >= 30:
        level = "MEDIUM"
    elif score >= 15:
        level = "LOW"
    else:
        level = "NORMAL"

    return {
        "uncached_prompt_tokens": uncached,
        "cache_ratio": cache_ratio,
        "token_byte_ratio": token_byte_ratio,
        "suspicion_score": score,
        "suspicion_level": level,
        "suspicion_flags": json.dumps(
            flags,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


# ============================================================
# OpenAI usage
# ============================================================

def extract_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}

    completion_details = (
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
        or {}
    )

    prompt_details = (
        usage.get("prompt_tokens_details")
        or usage.get("input_tokens_details")
        or {}
    )

    cache_read = (
        usage.get("cache_read_input_tokens")
        or usage.get("cached_tokens")
        or prompt_details.get("cache_read_input_tokens")
        or prompt_details.get("cached_tokens")
    )

    cache_creation = (
        usage.get("cache_creation_input_tokens")
        or usage.get("cache_write_input_tokens")
        or prompt_details.get("cache_creation_input_tokens")
        or prompt_details.get("cache_write_input_tokens")
    )

    prompt_tokens = safe_int(
        usage.get("prompt_tokens")
        if usage.get("prompt_tokens") is not None
        else usage.get("input_tokens")
    )

    completion_tokens = safe_int(
        usage.get("completion_tokens")
        if usage.get("completion_tokens") is not None
        else usage.get("output_tokens")
    )

    cached_tokens = safe_int(cache_read)
    cache_read_tokens = cached_tokens
    cache_creation_tokens = safe_int(cache_creation)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": safe_int(usage.get("total_tokens")),
        "reasoning_tokens": safe_int(
            completion_details.get("reasoning_tokens")
        ),
        "cached_tokens": cached_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "raw_usage_json": json.dumps(
            usage,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def parse_json_response(
    body: bytes,
) -> tuple[dict[str, Any], str | None, str | None]:
    try:
        payload = json.loads(body)
    except Exception:
        return {}, None, None

    if not isinstance(payload, dict):
        return {}, None, None

    usage = extract_usage(payload.get("usage"))

    finish_reason = None
    choices = payload.get("choices")

    if (
        isinstance(choices, list)
        and choices
        and isinstance(choices[0], dict)
    ):
        finish_reason = choices[0].get("finish_reason")

    raw_error = None
    if payload.get("error"):
        raw_error = json.dumps(
            payload["error"],
            ensure_ascii=False,
        )

    return usage, finish_reason, raw_error


def inject_stream_usage(
    payload: dict[str, Any],
) -> dict[str, Any]:
    result = dict(payload)

    if not result.get("stream"):
        return result

    existing = result.get("stream_options")

    if isinstance(existing, dict):
        existing = dict(existing)
        existing.setdefault("include_usage", True)
        result["stream_options"] = existing
    elif existing is None:
        result["stream_options"] = {
            "include_usage": True
        }

    return result


# ============================================================
# 速度
# ============================================================

def calc_rates(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    duration_ms: float | None,
    ttft_ms: float | None,
) -> tuple[float | None, float | None]:
    if not duration_ms or duration_ms <= 0:
        return None, None

    output_rate = None
    input_rate = None

    if completion_tokens is not None and completion_tokens >= 0:
        gen_sec = max(
            (duration_ms - (ttft_ms or 0.0)) / 1000.0,
            0.001,
        )
        output_rate = completion_tokens / gen_sec

    if prompt_tokens is not None and prompt_tokens >= 0:
        input_sec = max(
            (ttft_ms or duration_ms) / 1000.0,
            0.001,
        )
        input_rate = prompt_tokens / input_sec

    return output_rate, input_rate


# ============================================================
# SSE
# ============================================================

def event_has_output_delta(
    obj: dict[str, Any],
) -> bool:
    choices = obj.get("choices")

    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue

            delta = (
                choice.get("delta")
                or choice.get("message")
                or {}
            )

            if not isinstance(delta, dict):
                continue

            for key in (
                "content",
                "reasoning_content",
                "tool_calls",
                "audio",
                "function_call",
            ):
                value = delta.get(key)
                if value not in (None, "", [], {}):
                    return True

    event_type = str(obj.get("type") or "")

    if event_type.endswith(".delta"):
        delta = obj.get("delta")

        if delta not in (None, "", [], {}):
            return True

        if obj.get("text") not in (None, ""):
            return True

    return False


def merge_sse_event(
    text: str,
    state: dict[str, Any],
    started_perf: float,
) -> None:
    data_lines = []

    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if not data_lines:
        return

    data_text = "\n".join(data_lines)

    if data_text == "[DONE]":
        return

    try:
        obj = json.loads(data_text)
    except Exception:
        return

    if not isinstance(obj, dict):
        return

    if isinstance(obj.get("usage"), dict):
        state["usage"] = extract_usage(obj["usage"])

    if obj.get("error"):
        state["raw_error"] = json.dumps(
            obj["error"],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    choices = obj.get("choices")

    if isinstance(choices, list) and choices:
        first = choices[0]

        if (
            isinstance(first, dict)
            and first.get("finish_reason") is not None
        ):
            state["finish_reason"] = first.get(
                "finish_reason"
            )

    if (
        state.get("ttft_ms") is None
        and event_has_output_delta(obj)
    ):
        state["ttft_ms"] = (
            time.perf_counter() - started_perf
        ) * 1000


# ============================================================
# Log Row
# ============================================================

def make_log_row(
    request_uuid: str,
    request: Request,
    started_at: str,
    **kwargs: Any,
) -> dict[str, Any]:
    return {
        "request_uuid": request_uuid,
        "created_at": started_at,
        "started_at": started_at,
        "finished_at": kwargs.get("finished_at"),
        "method": request.method,
        "path": request.url.path,
        "public_model": kwargs.get("public_model"),
        "stream": 1 if kwargs.get("stream") else 0,
        "status_code": kwargs.get("status_code"),
        "request_bytes": kwargs.get("request_bytes", 0),
        "response_bytes": kwargs.get("response_bytes", 0),
        "client_body_bytes": kwargs.get(
            "client_body_bytes",
            kwargs.get("request_bytes", 0),
        ),
        "forwarded_body_bytes": kwargs.get(
            "forwarded_body_bytes",
            kwargs.get("request_bytes", 0),
        ),
        "prompt_tokens": kwargs.get("prompt_tokens"),
        "completion_tokens": kwargs.get("completion_tokens"),
        "total_tokens": kwargs.get("total_tokens"),
        "reasoning_tokens": kwargs.get("reasoning_tokens"),
        "cached_tokens": kwargs.get("cached_tokens"),
        "cache_read_tokens": kwargs.get(
            "cache_read_tokens",
            kwargs.get("cached_tokens"),
        ),
        "cache_creation_tokens": kwargs.get(
            "cache_creation_tokens"
        ),
        "uncached_prompt_tokens": kwargs.get(
            "uncached_prompt_tokens"
        ),
        "cache_ratio": kwargs.get("cache_ratio"),
        "token_byte_ratio": kwargs.get("token_byte_ratio"),
        "estimated_client_tokens": kwargs.get(
            "estimated_client_tokens"
        ),
        "payload_messages": kwargs.get(
            "payload_messages"
        ),
        "system_messages": kwargs.get(
            "system_messages"
        ),
        "developer_messages": kwargs.get(
            "developer_messages"
        ),
        "user_messages": kwargs.get(
            "user_messages"
        ),
        "assistant_messages": kwargs.get(
            "assistant_messages"
        ),
        "tool_messages": kwargs.get(
            "tool_messages"
        ),
        "tools_count": kwargs.get("tools_count"),
        "tool_schema_bytes": kwargs.get(
            "tool_schema_bytes"
        ),
        "system_prompt_chars": kwargs.get(
            "system_prompt_chars"
        ),
        "developer_prompt_chars": kwargs.get(
            "developer_prompt_chars"
        ),
        "message_content_chars": kwargs.get(
            "message_content_chars"
        ),
        "payload_sha256": kwargs.get(
            "payload_sha256"
        ),
        "suspicion_score": kwargs.get(
            "suspicion_score"
        ),
        "suspicion_level": kwargs.get(
            "suspicion_level"
        ),
        "suspicion_flags": kwargs.get(
            "suspicion_flags"
        ),
        "ttft_ms": kwargs.get("ttft_ms"),
        "duration_ms": kwargs.get("duration_ms"),
        "output_tokens_per_sec": kwargs.get(
            "output_tokens_per_sec"
        ),
        "input_tokens_per_sec": kwargs.get(
            "input_tokens_per_sec"
        ),
        "error": kwargs.get("error"),
        "client_ip": (
            request.client.host
            if request.client
            else None
        ),
        "user_agent": request.headers.get(
            "user-agent"
        ),
        "finish_reason": kwargs.get(
            "finish_reason"
        ),
        "raw_usage_json": kwargs.get(
            "raw_usage_json"
        ),
        "raw_error_json": kwargs.get(
            "raw_error_json"
        ),
        "payload_structure_json": kwargs.get(
            "payload_structure_json"
        ),
    }


# ============================================================
# Dashboard HTML
# ============================================================

HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gateway Logs</title>

<style>
:root{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    color:#17202a;
    background:#f4f6f8
}

*{
    box-sizing:border-box
}

body{
    margin:0
}

.container{
    max-width:1800px;
    margin:20px auto;
    padding:0 16px
}

.card{
    background:#fff;
    border:1px solid #e4e7eb;
    border-radius:14px;
    padding:16px;
    margin-bottom:14px;
    box-shadow:0 2px 12px rgba(0,0,0,.035)
}

h1{
    font-size:23px;
    margin:0 0 5px
}

h2{
    font-size:18px;
    margin:0 0 10px
}

.muted{
    color:#6b7280;
    font-size:12px
}

.toolbar{
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    align-items:end
}

.field{
    min-width:150px;
    flex:1
}

.field.wide{
    min-width:240px
}

label{
    display:block;
    color:#59636e;
    font-size:12px;
    margin:0 0 5px
}

select,
input,
button{
    font:inherit
}

select,
input{
    width:100%;
    padding:9px 10px;
    border:1px solid #d5dae0;
    border-radius:9px;
    background:#fff
}

button{
    padding:9px 13px;
    border:0;
    border-radius:9px;
    background:#111827;
    color:#fff;
    cursor:pointer
}

button:hover{
    opacity:.9
}

.secondary{
    background:#eef1f4;
    color:#111827
}

.danger{
    background:#b91c1c;
    color:#fff
}

.small-btn{
    padding:5px 8px;
    font-size:11px
}

.stats{
    display:grid;
    grid-template-columns:repeat(7,1fr);
    gap:10px
}

.stat{
    padding:13px;
    border:1px solid #edf0f2;
    border-radius:12px;
    background:#fafbfc;
    min-height:72px
}

.stat.alert-stat{
    background:#fffafa;
    border-color:#fee2e2
}

.stat .v{
    font-size:20px;
    font-weight:700;
    line-height:1.2
}

.stat .k{
    font-size:11px;
    color:#707a84;
    margin-top:6px
}

.stat .sub{
    font-size:10px;
    color:#9aa1a9;
    margin-top:2px
}

/* ============================================================
   最近日志
   ============================================================ */

.log-toolbar{
    display:flex;
    justify-content:space-between;
    align-items:center;
    gap:10px;
    flex-wrap:wrap;
    margin-bottom:10px
}

.log-toolbar-left,
.log-toolbar-right{
    display:flex;
    gap:7px;
    align-items:center;
    flex-wrap:wrap
}

.selection-info{
    color:#66707b;
    font-size:12px
}

.table-wrap{
    overflow:auto;
    border:1px solid #edf0f2;
    border-radius:10px
}

table{
    width:100%;
    border-collapse:separate;
    border-spacing:0;
    min-width:1180px
}

th,
td{
    text-align:left;
    padding:8px;
    border-bottom:1px solid #edf0f2;
    font-size:12px;
    vertical-align:middle;
    white-space:nowrap
}

th{
    color:#66707b;
    background:#fafbfc;
    position:sticky;
    top:0;
    z-index:2
}

tbody tr:hover td{
    background:#fafbfc
}

tbody tr.selected td{
    background:#f5f7fa
}

.checkbox-cell{
    width:38px;
    text-align:center
}

.checkbox-cell input{
    width:15px;
    height:15px;
    margin:0;
    cursor:pointer
}

.badge{
    display:inline-block;
    padding:3px 8px;
    border-radius:999px;
    background:#eef2ff;
    color:#3730a3;
    max-width:240px;
    overflow:hidden;
    text-overflow:ellipsis;
    vertical-align:middle
}

.status-ok{
    color:#166534;
    font-weight:600
}

.status-bad{
    color:#b91c1c;
    font-weight:700
}

.level-normal{
    color:#166534
}

.level-low{
    color:#92400e;
    font-weight:600
}

.level-medium{
    color:#c2410c;
    font-weight:700
}

.level-high{
    color:#b91c1c;
    font-weight:700
}

.suspicion-badge{
    display:inline-block;
    padding:3px 7px;
    border-radius:999px
}

.suspicion-normal{
    background:#ecfdf5;
    color:#166534
}

.suspicion-low{
    background:#fffbeb;
    color:#92400e
}

.suspicion-medium{
    background:#fff7ed;
    color:#c2410c
}

.suspicion-high{
    background:#fef2f2;
    color:#b91c1c
}

.metric-main{
    font-weight:700
}

.metric-cache{
    font-weight:600
}

.metric-danger{
    color:#b91c1c;
    font-weight:700
}

.mono{
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace
}

.copy-cell{
    text-align:center
}

.row-actions{
    display:flex;
    gap:5px;
    justify-content:center
}

.row-card-panel{
    display:grid;
    grid-template-columns:repeat(3,minmax(0,1fr));
    gap:9px 12px;
    padding:12px;
    background:#fff
}

.row-card-item{
    min-width:0
}

.row-card-item .k{
    color:#7b8490;
    font-size:10px;
    margin-bottom:3px
}

.row-card-item .v{
    color:#111827;
    font-size:12px;
    font-weight:600;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap
}

.floating-row-card{
    position:fixed;
    top:76px;
    right:16px;
    width:min(430px,calc(100vw - 32px));
    max-height:calc(100vh - 96px);
    overflow:auto;
    background:#fff;
    border:1px solid #dfe3e8;
    border-radius:14px;
    box-shadow:0 14px 40px rgba(15,23,42,.18);
    z-index:1000
}

.floating-row-card-head{
    position:sticky;
    top:0;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    padding:10px 12px;
    background:rgba(255,255,255,.96);
    border-bottom:1px solid #edf0f2;
    backdrop-filter:blur(8px);
    z-index:2
}

.floating-row-card-title{
    min-width:0;
    font-size:13px;
    font-weight:700;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap
}

.floating-row-card-close{
    flex:0 0 auto;
    padding:5px 8px;
    border-radius:8px;
    background:#eef1f4;
    color:#111827
}

.floating-row-card .row-card-panel{
    border:0;
    border-radius:0
}

.detail{
    white-space:pre-wrap;
    background:#0f172a;
    color:#dbeafe;
    padding:12px;
    border-radius:9px;
    max-height:650px;
    overflow:auto;
    font:12px/1.55 ui-monospace,monospace
}

.table-note{
    margin-top:8px;
    color:#8a929b;
    font-size:11px
}

/* ============================================================
   趋势
   ============================================================ */

.chart-wrap{
    overflow:hidden
}

.chart{
    display:flex;
    gap:7px;
    align-items:end;
    height:190px;
    padding:12px 4px 4px;
    min-width:0;
    overflow-x:auto;
    padding-left:0;
    padding-right:0
}

.bar-col{
    height:100%;
    display:flex;
    flex-direction:column;
    justify-content:end;
    align-items:center;
    min-width:32px
}

.bar{
    width:26px;
    border-radius:6px 6px 2px 2px;
    background:#4b5563
}

.bar.alert{
    background:#b91c1c
}

.x{
    font-size:9px;
    color:#7b8490;
    margin-top:5px;
    white-space:nowrap;
    transform:rotate(-35deg)
}

@media(max-width:1450px){
    .stats{
        grid-template-columns:repeat(5,1fr)
    }
}

@media(max-width:1000px){
    .stats{
        grid-template-columns:repeat(3,1fr)
    }
}

@media(max-width:700px){
    .stats{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px}
    .stat{padding:10px;min-height:66px}
    .stat .v{font-size:17px}
    .field{min-width:100%;flex:auto}
    .container{padding:0 9px;margin:10px auto}
    .card{padding:12px;border-radius:12px;margin-bottom:10px}
    h1{font-size:20px}
    h2{font-size:16px}
    .toolbar{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .toolbar .field,.toolbar .field.wide{min-width:0}
    .toolbar>div:last-child{grid-column:1/-1}
    .toolbar>div:nth-last-child(2){grid-column:auto}
    .toolbar button{width:100%}
    .desktop-only{display:none!important}
    .mobile-only{display:block}
    .log-toolbar{align-items:flex-start}
    .log-toolbar-right{width:100%;display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
    .log-toolbar-right button{width:100%}
    .table-note{font-size:10px;line-height:1.5}
    .detail{max-height:55vh;font-size:11px}
    .row-card-panel{grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;padding:9px}
    .row-card-item .v{font-size:11px}
    .row-actions{flex-direction:column}
    .row-actions button{width:100%}
}

.floating-row-card{}

@media(max-width:700px){
    .floating-row-card{
        top:10px;
        right:10px;
        width:calc(100vw - 20px);
        max-height:calc(100vh - 20px);
        border-radius:12px
    }

    .row-card-panel{
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:8px 10px
    }
}

</style>
</head>

<body>

<div class="container">

<!-- ============================================================
     页面标题 + 筛选
     ============================================================ -->

<div class="card">

<h1>Gateway Logs</h1>

<div class="muted">
最近请求优先。所有列均不固定；通过行首“展开”按钮查看悬浮信息卡，避免误触整行。
</div>

</div>


<div class="card">

<div class="toolbar">

<div class="field">
<label>时间范围</label>

<select id="range">
<option value="1h">最近 1 小时</option>
<option value="6h">最近 6 小时</option>
<option value="24h" selected>最近 24 小时</option>
<option value="7d">最近 7 天</option>
<option value="30d">最近 30 天</option>
<option value="custom">自定义</option>
</select>

</div>


<div class="field">
<label>开始</label>
<input id="start" type="datetime-local">
</div>


<div class="field">
<label>结束</label>
<input id="end" type="datetime-local">
</div>


<div class="field wide">
<label>模型</label>
<input id="model" placeholder="例如 openai/gpt-5">
</div>


<div>
<button onclick="loadAll()">刷新</button>
</div>


<div>
<button class="secondary" onclick="cleanup()">
清理旧日志
</button>
</div>

</div>

</div>


<!-- ============================================================
     最近详细日志：页面第一主区域
     ============================================================ -->

<div class="card">

<div class="log-toolbar">

<div class="log-toolbar-left">

<strong>最近请求</strong>

<span
    id="selectionInfo"
    class="selection-info"
>
已选择 0 条
</span>

</div>


<div class="log-toolbar-right">

<button
    class="secondary small-btn"
    onclick="selectAllVisible()"
>
全选
</button>

<button
    class="secondary small-btn"
    onclick="clearSelection()"
>
取消选择
</button>

<button
    class="small-btn"
    onclick="copySelectedRows()"
>
复制选中
</button>

</div>

</div>


<div class="table-wrap">

<table>

<thead>

<tr>

<th class="checkbox-cell">
<input
    id="selectAllCheckbox"
    type="checkbox"
    onclick="toggleAllVisible(this.checked)"
>
</th>

<th>展开</th>
<th>时间</th>
<th>模型</th>

<th>状态</th>

<!-- 核心 Token -->
<th>Prompt</th>
<th>缓存</th>
<th>缓存率</th>
<th>未缓存</th>

<!-- 异常 -->
<th>异常评分</th>
<th>等级</th>

<!-- 请求规模 -->
<th>请求体</th>
<th>Token/Byte</th>

<!-- 性能 -->
<th>输出</th>
<th>总 Token</th>
<th>TTFT</th>
<th>总用时</th>
<th>输出速度</th>

<!-- 请求结构 -->
<th>消息</th>
<th>System</th>
<th>Developer</th>
<th>Tools</th>
<th>正文字符</th>
<th>流式</th>
<th>操作</th>

</tr>

</thead>

<tbody id="logs"></tbody>

</table>

</div>


<div class="table-note">
所有列都不固定；点击行首“展开”按钮，以悬浮小卡片查看该行完整摘要。
</div>

</div>


<!-- ============================================================
     请求详情
     ============================================================ -->

<div
    class="card"
    id="detailCard"
    style="display:none"
>

<h2>请求详情</h2>

<div
    class="detail"
    id="detail"
></div>

</div>


<!-- ============================================================
     总览统计：放在详细日志之后
     ============================================================ -->

<div class="card">

<h2>总体统计</h2>

<div
    class="stats"
    id="stats"
></div>

</div>


<!-- ============================================================
     模型统计
     ============================================================ -->

<div class="card">

<h2>模型统计</h2>

<div class="desktop-only">

<div class="desktop-only">
<div class="table-wrap">

<table>

<thead>

<tr>

<th>模型</th>
<th>请求数</th>

<th>输入 Token</th>
<th>缓存读取</th>
<th>缓存率</th>
<th>未缓存</th>

<th>输出 Token</th>
<th>总 Token</th>

<th>最大 Prompt</th>

<th>HIGH</th>
<th>MEDIUM</th>
<th>错误</th>

<th>平均用时</th>
<th>输出速度</th>
<th>平均 Prompt/Byte</th>

</tr>

</thead>

<tbody id="models"></tbody>

</table>
</div>
<div class="mobile-only">
    <div id="modelCards" class="log-card-list"></div>
</div>

</div>

</div>


<!-- ============================================================
     趋势：最后
     ============================================================ -->

<div class="card">

<h2>调用趋势</h2>

<div class="chart-wrap">
<div
    class="chart"
    id="chart"
></div>
</div>
</div>

</div>

<div class="mobile-only">
    <div class="log-card-list" id="logCards"></div>
</div>

<div class="table-note">
柱高表示请求数量；红色表示存在 HIGH 异常请求。
</div>

</div>

</div>


<script>
let currentLogs=[];
let selectedRows=new Set();


function esc(v){
    return String(v??'').replace(
        /[&<>'"]/g,
        c=>({
            '&':'&amp;',
            '<':'&lt;',
            '>':'&gt;',
            "'":'&#39;',
            '"':'&quot;'
        }[c])
    );
}


function num(v){
    return v==null
        ? '—'
        : Number(v).toLocaleString();
}


function dec(v,d=1){
    return v==null
        ? '—'
        : Number(v).toFixed(d);
}


function pct(v){
    return v==null
        ? '—'
        : (Number(v)*100).toFixed(1)+'%';
}


function dur(v){
    if(v==null)return '—';

    return v<1000
        ? v.toFixed(0)+' ms'
        : (v/1000).toFixed(2)+' s';
}


function rate(v){
    return v==null
        ? '—'
        : Number(v).toFixed(1)+' tok/s';
}


function localInput(d){
    const x=new Date(d);

    return new Date(
        x.getTime()-x.getTimezoneOffset()*60000
    ).toISOString().slice(0,16);
}


function rangeParams(){

    const r=
        document.getElementById(
            'range'
        ).value;

    if(r!=='custom'){

        return {
            range:r,
            model:
                document.getElementById(
                    'model'
                ).value.trim()
        };

    }

    return {

        start:
            new Date(
                document.getElementById(
                    'start'
                ).value
            ).toISOString(),

        end:
            new Date(
                document.getElementById(
                    'end'
                ).value
            ).toISOString(),

        model:
            document.getElementById(
                'model'
            ).value.trim()

    };
}


async function api(
    url,
    options={}
){

    const h=
        await fetch(
            url,
            options
        );

    const t=
        await h.text();

    let d={};

    try{
        d=t?JSON.parse(t):{};
    }catch{
        d={detail:t};
    }

    if(!h.ok){

        throw new Error(
            d.detail
            || `HTTP ${h.status}`
        );

    }

    return d;
}


function setDefaults(){

    const end=
        new Date();

    const start=
        new Date(
            end.getTime()
            -24*3600*1000
        );

    document.getElementById(
        'start'
    ).value=
        localInput(
            start.toISOString()
        );

    document.getElementById(
        'end'
    ).value=
        localInput(
            end.toISOString()
        );
}


async function loadAll(){

    try{

        const p=
            rangeParams();

        const q=
            new URLSearchParams(
                p
            ).toString();

        const [
            summary,
            models,
            logs
        ]=
            await Promise.all([

                api(
                    '/api/logs/summary?'+q
                ),

                api(
                    '/api/logs/models?'+q
                ),

                api(
                    '/api/logs?'+q
                )

            ]);

        renderSummary(
            summary
        );

        renderModels(
            models
        );

        currentLogs=
            logs.items||[];

        // 数据刷新后清除不存在的旧选择
        selectedRows=
            new Set(
                [...selectedRows].filter(
                    i=>i<currentLogs.length
                )
            );

        renderLogs(
            currentLogs
        );

        renderChart(
            summary.buckets||[]
        );

    }catch(e){

        alert(
            e.message
        );

    }

}


/* ============================================================
   最近日志
   ============================================================ */


function renderLogs(rows){

    document.getElementById(
        'logs'
    ).innerHTML=

        (rows||[]).map(

            (r,i)=>{

                const level=
                    String(
                        r.suspicion_level
                        ||'NORMAL'
                    ).toLowerCase();

                const scoreClass=
                    level==='high'
                        ?'metric-danger'
                        :'';

                const statusClass=
                    Number(r.status_code)>=400
                        ?'status-bad'
                        :'status-ok';

                const selected=
                    selectedRows.has(i);

                return `

                <tr
                    id="row-${i}"
                    class="${selected?'selected':''}"
                >

                    <td
                        class="checkbox-cell"
                        onclick="event.stopPropagation()"
                    >

                        <input
                            type="checkbox"
                            ${selected?'checked':''}
                            onchange="toggleRow(${i},this.checked)"
                        >

                    </td>


                    <td class="copy-cell">
                        <button
                            class="small-btn row-card-btn"
                            onclick="toggleRowCard(${i})"
                            aria-expanded="false"
                            title="查看该行信息"
                        >展开</button>
                    </td>

                    <td>
                        ${esc(
                            new Date(
                                r.created_at
                            ).toLocaleString()
                        )}
                    </td>

                    <td>

                        <span class="badge">

                            ${esc(shortModel(r.public_model))}

                        </span>

                    </td>


                    <td class="${statusClass}">
                        ${num(r.status_code)}
                    </td>


                    <!-- 核心 Token -->

                    <td class="metric-main">
                        ${num(r.prompt_tokens)}
                    </td>

                    <td class="metric-cache">
                        ${num(r.cache_read_tokens)}
                    </td>

                    <td class="metric-cache">
                        ${pct(r.cache_ratio)}
                    </td>

                    <td>
                        ${num(r.uncached_prompt_tokens)}
                    </td>


                    <!-- 异常 -->

                    <td class="${scoreClass}">
                        ${num(r.suspicion_score)}
                    </td>

                    <td>

                        <span
                            class="
                                suspicion-badge
                                suspicion-${level}
                            "
                        >
                            ${esc(
                                r.suspicion_level
                                ||'NORMAL'
                            )}
                        </span>

                    </td>


                    <!-- 请求规模 -->

                    <td class="mono">
                        ${num(r.client_body_bytes)} B
                    </td>

                    <td class="mono">
                        ${dec(r.token_byte_ratio,2)}
                    </td>


                    <!-- 性能 -->

                    <td>
                        ${num(r.completion_tokens)}
                    </td>

                    <td>
                        ${num(r.total_tokens)}
                    </td>

                    <td>
                        ${dur(r.ttft_ms)}
                    </td>

                    <td>
                        ${dur(r.duration_ms)}
                    </td>

                    <td>
                        ${rate(r.output_tokens_per_sec)}
                    </td>


                    <!-- 请求结构 -->

                    <td>
                        ${num(r.payload_messages)}
                    </td>

                    <td>
                        ${num(r.system_messages)}
                    </td>

                    <td>
                        ${num(r.developer_messages)}
                    </td>

                    <td>
                        ${num(r.tools_count)}
                    </td>

                    <td>
                        ${num(r.message_content_chars)}
                    </td>

                    <td>
                        ${r.stream?'是':'否'}
                    </td>


                    <!-- 操作 -->

                    <td class="copy-cell">
                        <button
                            class="secondary small-btn"
                            onclick="copySingleRow(${i})"
                        >复制</button>
                    </td>

                </tr>

                `;

            }

        ).join('')

        ||

        '<tr><td colspan="24">暂无日志</td></tr>';


    renderLogCards(rows||[]);
    updateSelectionUI();
}


function renderLogCards(rows){
    const box=document.getElementById('logCards');
    if(!box)return;

    box.innerHTML=(rows||[]).map((r,i)=>{
        const level=String(r.suspicion_level||'NORMAL').toLowerCase();
        const statusClass=Number(r.status_code)>=400?'status-bad':'status-ok';
        const selected=selectedRows.has(i);
        return `
        <div class="log-card ${selected?'selected':''}">
            <div class="log-card-top">
                <div class="log-card-title">
                    <div class="muted">${esc(new Date(r.created_at).toLocaleString())}</div>
                    <div style="margin-top:4px"><span class="badge" title="${esc(r.public_model||'—')}">${esc(shortModel(r.public_model))}</span></div>
                </div>
                <span class="${statusClass}">${num(r.status_code)}</span>
            </div>

            <div class="log-card-meta">
                <div class="log-metric"><div class="k">Prompt</div><div class="v">${num(r.prompt_tokens)}</div></div>
                <div class="log-metric"><div class="k">缓存读取</div><div class="v">${num(r.cache_read_tokens)} · ${pct(r.cache_ratio)}</div></div>
                <div class="log-metric"><div class="k">未缓存</div><div class="v">${num(r.uncached_prompt_tokens)}</div></div>
                <div class="log-metric"><div class="k">输出</div><div class="v">${num(r.completion_tokens)} · ${rate(r.output_tokens_per_sec)}</div></div>
                <div class="log-metric"><div class="k">TTFT</div><div class="v">${dur(r.ttft_ms)}</div></div>
                <div class="log-metric"><div class="k">总用时</div><div class="v">${dur(r.duration_ms)}</div></div>
                <div class="log-metric"><div class="k">消息 / System</div><div class="v">${num(r.payload_messages)} / ${num(r.system_messages)}</div></div>
                <div class="log-metric"><div class="k">Developer / Tools</div><div class="v">${num(r.developer_messages)} / ${num(r.tools_count)}</div></div>
                <div class="log-metric"><div class="k">异常</div><div class="v ${level==='high'?'metric-danger':''}">${num(r.suspicion_score)} · ${esc(r.suspicion_level||'NORMAL')}</div></div>
                <div class="log-metric"><div class="k">正文字符</div><div class="v">${num(r.message_content_chars)}</div></div>
            </div>

            <div class="log-card-actions">
                <label style="display:flex;align-items:center;gap:7px;margin:0">
                    <input type="checkbox" ${selected?'checked':''} onchange="toggleRow(${i},this.checked)">
                    <span class="muted">选择</span>
                </label>
                <div style="display:flex;gap:6px">
                    <button class="secondary small-btn" onclick="copySingleRow(${i})">复制</button>
                    <button class="small-btn detail-btn" onclick="toggleRowCard(${i})">展开</button>
                </div>
            </div>
        </div>`;
    }).join('') || '<div class="muted">暂无日志</div>';
}


function shortModel(v){
    const s=String(v||'—');
    return s.includes('/') ? s.split('/').pop() : s;
}

function rowCardHtml(r){
    return `
        <div class="row-card-panel">
            <div class="row-card-item"><div class="k">完整模型</div><div class="v" title="${esc(r.public_model||'—')}">${esc(r.public_model||'—')}</div></div>
            <div class="row-card-item"><div class="k">时间</div><div class="v">${esc(new Date(r.created_at).toLocaleString())}</div></div>
            <div class="row-card-item"><div class="k">状态</div><div class="v">${num(r.status_code)}</div></div>
            <div class="row-card-item"><div class="k">Prompt</div><div class="v">${num(r.prompt_tokens)}</div></div>
            <div class="row-card-item"><div class="k">缓存读取</div><div class="v">${num(r.cache_read_tokens)} · ${pct(r.cache_ratio)}</div></div>
            <div class="row-card-item"><div class="k">未缓存</div><div class="v">${num(r.uncached_prompt_tokens)}</div></div>
            <div class="row-card-item"><div class="k">输出</div><div class="v">${num(r.completion_tokens)}</div></div>
            <div class="row-card-item"><div class="k">总 Token</div><div class="v">${num(r.total_tokens)}</div></div>
            <div class="row-card-item"><div class="k">TTFT</div><div class="v">${dur(r.ttft_ms)}</div></div>
            <div class="row-card-item"><div class="k">总用时</div><div class="v">${dur(r.duration_ms)}</div></div>
            <div class="row-card-item"><div class="k">输出速度</div><div class="v">${rate(r.output_tokens_per_sec)}</div></div>
            <div class="row-card-item"><div class="k">消息 / System</div><div class="v">${num(r.payload_messages)} / ${num(r.system_messages)}</div></div>
            <div class="row-card-item"><div class="k">Developer / Tools</div><div class="v">${num(r.developer_messages)} / ${num(r.tools_count)}</div></div>
            <div class="row-card-item"><div class="k">请求体</div><div class="v">${num(r.client_body_bytes)} B</div></div>
            <div class="row-card-item"><div class="k">Token/Byte</div><div class="v">${dec(r.token_byte_ratio,2)}</div></div>
            <div class="row-card-item"><div class="k">正文字符</div><div class="v">${num(r.message_content_chars)}</div></div>
            <div class="row-card-item"><div class="k">异常</div><div class="v">${esc(r.suspicion_level||'NORMAL')} · ${num(r.suspicion_score)}</div></div>
            <div class="row-card-item"><div class="k">流式</div><div class="v">${r.stream?'是':'否'}</div></div>
        </div>`;
}

function closeRowCard(){
    const floating=document.getElementById('floatingRowCard');
    if(floating) floating.remove();
    document.querySelectorAll('.row-card-btn').forEach(btn=>{
        btn.textContent='展开';
        btn.setAttribute('aria-expanded','false');
    });
}

function toggleRowCard(i){
    const existing=document.getElementById('floatingRowCard');
    if(existing && existing.dataset.index===String(i)){
        closeRowCard();
        return;
    }

    closeRowCard();
    if(!currentLogs[i]) return;

    const r=currentLogs[i];
    const floating=document.createElement('div');
    floating.id='floatingRowCard';
    floating.className='floating-row-card';
    floating.dataset.index=String(i);
    floating.innerHTML=`
        <div class="floating-row-card-head">
            <div class="floating-row-card-title">第 ${i+1} 条请求 · ${esc(shortModel(r.public_model))}</div>
            <button class="floating-row-card-close" onclick="closeRowCard()">关闭</button>
        </div>
        ${rowCardHtml(r)}
    `;
    document.body.appendChild(floating);

    document.querySelectorAll('.row-card-btn').forEach(btn=>{
        btn.setAttribute('aria-expanded','false');
        btn.textContent='展开';
    });
    const row=document.getElementById(`row-${i}`);
    const button=row ? row.querySelector('.row-card-btn') : null;
    if(button){
        button.textContent='收起';
        button.setAttribute('aria-expanded','true');
    }
}

function toggleRow(
    index,
    checked
){

    if(checked){
        selectedRows.add(index);
    }else{
        selectedRows.delete(index);
    }

    renderLogs(currentLogs);
}


function selectAllVisible(){

    for(
        let i=0;
        i<currentLogs.length;
        i++
    ){
        selectedRows.add(i);
    }

    renderLogs(currentLogs);
}


function clearSelection(){

    selectedRows.clear();

    renderLogs(currentLogs);
}


function toggleAllVisible(
    checked
){

    if(checked){

        for(
            let i=0;
            i<currentLogs.length;
            i++
        ){
            selectedRows.add(i);
        }

    }else{

        selectedRows.clear();

    }

    renderLogs(currentLogs);
}


function updateSelectionUI(){

    const count=
        selectedRows.size;

    document.getElementById(
        'selectionInfo'
    ).textContent=
        `已选择 ${count} 条`;

    const checkbox=
        document.getElementById(
            'selectAllCheckbox'
        );

    if(!checkbox){
        return;
    }

    checkbox.checked=
        currentLogs.length>0
        && count===currentLogs.length;

    checkbox.indeterminate=
        count>0
        && count<currentLogs.length;
}


/* ============================================================
   复制
   ============================================================ */

const COPY_HEADERS=[

    '时间',
    '模型',
    '状态',

    'Prompt',
    '缓存',
    '缓存率',
    '未缓存',

    '异常评分',
    '等级',

    '请求体',
    'Token/Byte',

    '输出',
    '总 Token',
    'TTFT',
    '总用时',
    '输出速度',

    '消息',
    'System',
    'Developer',
    'Tools',
    '正文字符',

    '流式'

];


function copyCellValue(
    value
){

    if(
        value===null
        ||
        value===undefined
        ||
        value===''
    ){
        return '';
    }

    return String(
        value
    )
    .replace(/\r?\n/g,' ')
    .replace(/\t/g,' ');
}


function rowToCopyArray(
    r
){

    return [

        new Date(
            r.created_at
        ).toLocaleString(),

        r.public_model||'',

        r.status_code??'',

        r.prompt_tokens??'',

        r.cache_read_tokens??'',

        r.cache_ratio==null
            ?''
            :(Number(r.cache_ratio)*100)
                .toFixed(1)+'%',

        r.uncached_prompt_tokens??'',

        r.suspicion_score??'',

        r.suspicion_level||'',

        r.client_body_bytes==null
            ?''
            :`${r.client_body_bytes} B`,

        r.token_byte_ratio==null
            ?''
            :Number(
                r.token_byte_ratio
            ).toFixed(2),

        r.completion_tokens??'',

        r.total_tokens??'',

        dur(r.ttft_ms),

        dur(r.duration_ms),

        r.output_tokens_per_sec==null
            ?''
            :Number(
                r.output_tokens_per_sec
            ).toFixed(1)+' tok/s',

        r.payload_messages??'',

        r.system_messages??'',

        r.developer_messages??'',

        r.tools_count??'',

        r.message_content_chars??'',

        r.stream?'是':'否'

    ].map(
        copyCellValue
    );
}


function buildCopyText(
    indexes
){

    const lines=[
        COPY_HEADERS
    ];

    for(
        const index of indexes
    ){

        const r=
            currentLogs[index];

        if(!r){
            continue;
        }

        lines.push(
            rowToCopyArray(r)
        );

    }

    return lines
        .map(
            row=>row.join('\t')
        )
        .join('\n');
}


async function writeClipboard(
    text
){

    try{

        await navigator.clipboard.writeText(
            text
        );

        return true;

    }catch{

        const textarea=
            document.createElement(
                'textarea'
            );

        textarea.value=text;

        textarea.style.position='fixed';
        textarea.style.left='-9999px';
        textarea.style.top='-9999px';

        document.body.appendChild(
            textarea
        );

        textarea.focus();
        textarea.select();

        let ok=false;

        try{
            ok=document.execCommand(
                'copy'
            );
        }catch{
            ok=false;
        }

        textarea.remove();

        return ok;
    }
}


async function copySingleRow(
    index
){

    const text=
        buildCopyText([
            index
        ]);

    const ok=
        await writeClipboard(
            text
        );

    if(ok){

        const button=
            document.querySelector(
                `button[onclick*="copySingleRow(${index})"]`
            );

        if(button){

            const old=
                button.textContent;

            button.textContent=
                '已复制';

            setTimeout(
                ()=>{
                    button.textContent=old;
                },
                1000
            );

        }

    }else{

        alert(
            '复制失败，请检查浏览器剪贴板权限。'
        );

    }
}


async function copySelectedRows(){

    const indexes=
        [...selectedRows]
        .sort(
            (a,b)=>a-b
        );

    if(!indexes.length){

        alert(
            '请先选择日志。'
        );

        return;
    }

    const text=
        buildCopyText(
            indexes
        );

    const ok=
        await writeClipboard(
            text
        );

    if(ok){

        alert(
            `已复制 ${indexes.length} 条日志（含表头）`
        );

    }else{

        alert(
            '复制失败，请检查浏览器剪贴板权限。'
        );

    }
}


/* ============================================================
   详情
   ============================================================ */

function showDetail(i){

    const r=
        currentLogs[i];

    document.getElementById(
        'detailCard'
    ).style.display='block';

    document.getElementById(
        'detail'
    ).textContent=
        JSON.stringify(
            r,
            null,
            2
        );

    document.getElementById(
        'detailCard'
    ).scrollIntoView({
        behavior:'smooth',
        block:'start'
    });
}


/* ============================================================
   总览统计
   ============================================================ */

function renderSummary(s){

    const cards=[

        [
            '请求数',
            num(s.requests),
            '总体调用量',
            ''
        ],

        [
            '输入 Token',
            num(s.prompt_tokens),
            '模型实际接收输入',
            ''
        ],

        [
            '缓存读取',
            num(s.cache_read_tokens),
            '重复前缀的重要指标',
            ''
        ],

        [
            '平均缓存率',
            pct(s.avg_cache_ratio),
            '固定上下文观察指标',
            ''
        ],

        [
            '未缓存 Token',
            num(s.uncached_prompt_tokens),
            '本次新增输入规模',
            ''
        ],

        [
            '异常请求',
            num(s.suspicious_requests),
            'LOW / MEDIUM / HIGH',
            s.suspicious_requests?'alert-stat':''
        ],

        [
            'HIGH',
            num(s.high_suspicious),
            '高风险异常信号',
            s.high_suspicious?'alert-stat':''
        ],

        [
            '输出 Token',
            num(s.completion_tokens),
            '模型生成量',
            ''
        ],

        [
            '总 Token',
            num(s.total_tokens),
            '输入 + 输出',
            ''
        ],

        [
            '最大 Prompt',
            num(s.max_prompt_tokens),
            '单次最大输入',
            ''
        ],

        [
            '平均 Prompt/Byte',
            dec(s.avg_token_byte_ratio,2),
            '请求放大观察',
            ''
        ],

        [
            'MEDIUM',
            num(s.medium_suspicious),
            '中度异常信号',
            ''
        ],

        [
            '平均用时',
            dur(s.avg_duration_ms),
            '端到端时间',
            ''
        ],

        [
            '平均输出速度',
            rate(s.avg_output_tokens_per_sec),
            '生成速度',
            ''
        ]

    ];

    document.getElementById(
        'stats'
    ).innerHTML=

        cards.map(
            x=>`

            <div class="stat ${x[3]}">

                <div class="v">
                    ${x[1]}
                </div>

                <div class="k">
                    ${x[0]}
                </div>

                <div class="sub">
                    ${x[2]}
                </div>

            </div>

            `
        ).join('');
}


/* ============================================================
   模型统计
   ============================================================ */

function renderModels(rows){

    document.getElementById(
        'models'
    ).innerHTML=

        (rows||[]).map(
            r=>`

            <tr>

                <td>

                    <span class="badge">

                        ${esc(
                            r.public_model
                            ||'未知'
                        )}

                    </span>

                </td>

                <td class="metric-main">
                    ${num(r.requests)}
                </td>

                <td class="metric-main">
                    ${num(r.prompt_tokens)}
                </td>

                <td class="metric-cache">
                    ${num(r.cache_read_tokens)}
                </td>

                <td class="metric-cache">
                    ${pct(r.avg_cache_ratio)}
                </td>

                <td>
                    ${num(r.uncached_prompt_tokens)}
                </td>

                <td>
                    ${num(r.completion_tokens)}
                </td>

                <td>
                    ${num(r.total_tokens)}
                </td>

                <td>
                    ${num(r.max_prompt_tokens)}
                </td>

                <td class="level-high">
                    ${num(r.high_suspicious)}
                </td>

                <td class="level-medium">
                    ${num(r.medium_suspicious)}
                </td>

                <td class="${r.errors?'status-bad':'status-ok'}">
                    ${num(r.errors)}
                </td>

                <td>
                    ${dur(r.avg_duration_ms)}
                </td>

                <td>
                    ${rate(r.avg_output_tokens_per_sec)}
                </td>

                <td class="mono">
                    ${dec(r.avg_token_byte_ratio,2)}
                </td>

            </tr>

            `
        ).join('')

        ||

        '<tr><td colspan="15">暂无数据</td></tr>';

    renderModelCards(rows||[]);
}


/* ============================================================
   趋势
   ============================================================ */

function renderModelCards(rows){
    const box=document.getElementById('modelCards');
    if(!box)return;
    box.innerHTML=(rows||[]).map(r=>`
        <div class="log-card">
            <div class="log-card-title"><span class="badge">${esc(r.public_model||'未知')}</span></div>
            <div class="log-card-meta">
                <div class="log-metric"><div class="k">请求数</div><div class="v">${num(r.requests)}</div></div>
                <div class="log-metric"><div class="k">输入 / 输出</div><div class="v">${num(r.prompt_tokens)} / ${num(r.completion_tokens)}</div></div>
                <div class="log-metric"><div class="k">缓存读取</div><div class="v">${num(r.cache_read_tokens)} · ${pct(r.avg_cache_ratio)}</div></div>
                <div class="log-metric"><div class="k">最大 Prompt</div><div class="v">${num(r.max_prompt_tokens)}</div></div>
                <div class="log-metric"><div class="k">平均用时</div><div class="v">${dur(r.avg_duration_ms)}</div></div>
                <div class="log-metric"><div class="k">输出速度</div><div class="v">${rate(r.avg_output_tokens_per_sec)}</div></div>
                <div class="log-metric"><div class="k">错误</div><div class="v">${num(r.errors)}</div></div>
                <div class="log-metric"><div class="k">异常</div><div class="v">HIGH ${num(r.high_suspicious)} · MEDIUM ${num(r.medium_suspicious)}</div></div>
            </div>
        </div>`).join('') || '<div class="muted">暂无数据</div>';
}


function renderChart(b){

    if(!b.length){

        document.getElementById(
            'chart'
        ).innerHTML=
            '<div class="muted">暂无数据</div>';

        return;
    }

    const max=
        Math.max(
            ...b.map(
                x=>x.requests
            ),
            1
        );

    document.getElementById(
        'chart'
    ).innerHTML=

        b.map(
            x=>`

            <div class="bar-col">

                <div
                    class="mono"
                    style="
                        font-size:9px;
                        margin-bottom:3px
                    "
                >
                    ${x.requests}
                </div>

                <div
                    class="
                        bar
                        ${x.high_suspicious?'alert':''}
                    "
                    style="
                        height:${
                            Math.max(
                                4,
                                (
                                    x.requests/max
                                )*140
                            )
                        }px
                    "
                    title="
                        请求 ${x.requests}
                        / HIGH ${x.high_suspicious||0}
                        / MEDIUM ${x.medium_suspicious||0}
                    "
                ></div>

                <div class="x">
                    ${esc(x.label)}
                </div>

            </div>

            `
        ).join('');
}


/* ============================================================
   清理
   ============================================================ */

async function cleanup(){

    const days=
        prompt(
            '保留最近多少天？默认 30',
            '30'
        );

    if(!days){
        return;
    }

    const numberDays=
        Number(days);

    if(
        !Number.isFinite(numberDays)
        ||
        numberDays<1
    ){

        alert(
            '请输入有效天数。'
        );

        return;
    }

    try{

        const r=
            await api(
                '/api/logs/cleanup',
                {
                    method:'POST',
                    headers:{
                        'Content-Type':
                            'application/json'
                    },
                    body:
                        JSON.stringify({
                            days:numberDays
                        })
                }
            );

        alert(
            `已删除 ${r.deleted} 条`
        );

        loadAll();

    }catch(e){

        alert(
            e.message
        );

    }
}


/* ============================================================
   初始化
   ============================================================ */

setDefaults();
loadAll();

</script>

</body>
</html>'''

# ============================================================
# FastAPI 生命周期
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    print("=" * 80)
    print("Gateway Log Sidecar")
    print(f"Dashboard : http://127.0.0.1:{PORT}")
    print(f"Proxy     : http://127.0.0.1:{PORT}/v1")
    print(f"Core      : {CORE_BASE_URL}")
    print(f"Log DB    : {LOG_DB_PATH}")
    print(f"Admin Key : {'ON' if LOG_ADMIN_KEY else 'OFF'}")
    print("=" * 80)

    yield


app = FastAPI(
    title="Gateway Log Sidecar",
    version="1.2.0",
    lifespan=lifespan,
)


# ============================================================
# Dashboard API
# ============================================================

def parse_window(
    start: str | None,
    end: str | None,
    range_value: str | None,
) -> tuple[str, str]:
    end_dt = (
        utc_now()
        if not end
        else datetime.fromisoformat(
            end.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    )

    if start:
        start_dt = datetime.fromisoformat(
            start.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    else:
        delta = {
            "1h": timedelta(hours=1),
            "6h": timedelta(hours=6),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
        }.get(
            range_value or "24h",
            timedelta(hours=24),
        )

        start_dt = end_dt - delta

    return (
        start_dt.isoformat(timespec="milliseconds"),
        end_dt.isoformat(timespec="milliseconds"),
    )


def model_clause(
    model: str | None,
    args: list[Any],
) -> str:
    if model:
        args.append(model)
        return " AND public_model = ?"
    return ""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(HTML)


@app.get("/api/logs/summary")
async def logs_summary(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    range: str = "24h",
    model: str | None = None,
) -> dict[str, Any]:
    check_admin(request)

    start_iso, end_iso = parse_window(
        start,
        end,
        range,
    )

    args: list[Any] = [start_iso, end_iso]
    extra = model_clause(model, args)

    conn = db()

    try:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS requests,

                SUM(
                    CASE
                        WHEN status_code BETWEEN 200 AND 399
                        THEN 1 ELSE 0
                    END
                ) AS successes,

                COALESCE(SUM(prompt_tokens), 0)
                    AS prompt_tokens,

                COALESCE(SUM(completion_tokens), 0)
                    AS completion_tokens,

                COALESCE(SUM(total_tokens), 0)
                    AS total_tokens,

                COALESCE(SUM(uncached_prompt_tokens), 0)
                    AS uncached_prompt_tokens,

                COALESCE(SUM(cache_read_tokens), 0)
                    AS cache_read_tokens,

                COALESCE(SUM(cache_creation_tokens), 0)
                    AS cache_creation_tokens,

                AVG(cache_ratio)
                    AS avg_cache_ratio,

                AVG(token_byte_ratio)
                    AS avg_token_byte_ratio,

                MAX(prompt_tokens)
                    AS max_prompt_tokens,

                AVG(duration_ms)
                    AS avg_duration_ms,

                AVG(output_tokens_per_sec)
                    AS avg_output_tokens_per_sec,

                SUM(
                    CASE
                        WHEN suspicion_level IN ('LOW','MEDIUM','HIGH')
                        THEN 1 ELSE 0
                    END
                ) AS suspicious_requests,

                SUM(
                    CASE
                        WHEN suspicion_level = 'HIGH'
                        THEN 1 ELSE 0
                    END
                ) AS high_suspicious,

                SUM(
                    CASE
                        WHEN suspicion_level = 'MEDIUM'
                        THEN 1 ELSE 0
                    END
                ) AS medium_suspicious

            FROM request_logs
            WHERE created_at >= ?
              AND created_at <= ?
              {extra}
            """,
            args,
        ).fetchone()

        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        days = (end_dt - start_dt).total_seconds() / 86400

        fmt = "%Y-%m-%d" if days > 3 else "%Y-%m-%d %H:00"

        bucket_args: list[Any] = [
            start_iso,
            end_iso,
        ]

        if model:
            bucket_args.append(model)

        rows = conn.execute(
            f"""
            SELECT
                strftime('{fmt}', created_at) AS label,
                COUNT(*) AS requests,
                SUM(
                    CASE
                        WHEN suspicion_level = 'HIGH'
                        THEN 1 ELSE 0
                    END
                ) AS high_suspicious,
                SUM(
                    CASE
                        WHEN suspicion_level = 'MEDIUM'
                        THEN 1 ELSE 0
                    END
                ) AS medium_suspicious
            FROM request_logs
            WHERE created_at >= ?
              AND created_at <= ?
              {extra}
            GROUP BY label
            ORDER BY label
            """,
            bucket_args,
        ).fetchall()

    finally:
        conn.close()

    return {
        **dict(row),
        "buckets": [dict(r) for r in rows],
    }


@app.get("/api/logs/models")
async def logs_models(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    range: str = "24h",
    model: str | None = None,
) -> list[dict[str, Any]]:
    check_admin(request)

    start_iso, end_iso = parse_window(
        start,
        end,
        range,
    )

    args: list[Any] = [start_iso, end_iso]
    extra = model_clause(model, args)

    conn = db()

    try:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(public_model, '未知')
                    AS public_model,

                COUNT(*) AS requests,

                COALESCE(SUM(total_tokens), 0)
                    AS total_tokens,

                COALESCE(SUM(prompt_tokens), 0)
                    AS prompt_tokens,

                COALESCE(SUM(completion_tokens), 0)
                    AS completion_tokens,

                COALESCE(SUM(uncached_prompt_tokens), 0)
                    AS uncached_prompt_tokens,

                COALESCE(SUM(cache_read_tokens), 0)
                    AS cache_read_tokens,

                COALESCE(SUM(cache_creation_tokens), 0)
                    AS cache_creation_tokens,

                AVG(cache_ratio)
                    AS avg_cache_ratio,

                MAX(prompt_tokens)
                    AS max_prompt_tokens,

                AVG(token_byte_ratio)
                    AS avg_token_byte_ratio,

                AVG(duration_ms)
                    AS avg_duration_ms,

                AVG(output_tokens_per_sec)
                    AS avg_output_tokens_per_sec,

                SUM(
                    CASE
                        WHEN suspicion_level = 'HIGH'
                        THEN 1 ELSE 0
                    END
                ) AS high_suspicious,

                SUM(
                    CASE
                        WHEN suspicion_level = 'MEDIUM'
                        THEN 1 ELSE 0
                    END
                ) AS medium_suspicious,

                SUM(
                    CASE
                        WHEN status_code >= 400
                        THEN 1 ELSE 0
                    END
                ) AS errors

            FROM request_logs
            WHERE created_at >= ?
              AND created_at <= ?
              {extra}
            GROUP BY public_model
            ORDER BY requests DESC
            """,
            args,
        ).fetchall()

        return [dict(r) for r in rows]

    finally:
        conn.close()


@app.get("/api/logs")
async def logs_list(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    range: str = "24h",
    model: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    check_admin(request)

    start_iso, end_iso = parse_window(
        start,
        end,
        range,
    )

    limit = max(1, min(limit, 5000))

    args: list[Any] = [start_iso, end_iso]
    extra = model_clause(model, args)
    args.append(limit)

    conn = db()

    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM request_logs
            WHERE created_at >= ?
              AND created_at <= ?
              {extra}
            ORDER BY id DESC
            LIMIT ?
            """,
            args,
        ).fetchall()

        return {
            "items": [dict(r) for r in rows],
            "count": len(rows),
        }

    finally:
        conn.close()


@app.post("/api/logs/cleanup")
async def logs_cleanup(request: Request) -> dict[str, Any]:
    check_admin(request)

    payload = await request.json()

    days = max(
        1,
        min(
            int(payload.get("days", 30)),
            3650,
        ),
    )

    cutoff = (
        utc_now() - timedelta(days=days)
    ).isoformat(timespec="milliseconds")

    return {
        "deleted": delete_logs_before(cutoff),
        "cutoff": cutoff,
    }


# ============================================================
# Sidecar Proxy
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


def forward_request_headers(
    request: Request,
) -> dict[str, str]:
    result: dict[str, str] = {}

    for k, v in request.headers.items():
        if k.lower() in HOP_BY_HOP:
            continue
        result[k] = v

    return result


def forward_response_headers(
    headers: httpx.Headers,
) -> dict[str, str]:
    result: dict[str, str] = {}

    for k, v in headers.items():
        if (
            k.lower() in HOP_BY_HOP
            or k.lower() == "content-length"
        ):
            continue
        result[k] = v

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
        "OPTIONS",
    ],
)
async def proxy(
    request: Request,
    path: str,
) -> Response:
    started_perf = time.perf_counter()
    started_at = now_iso()
    request_uuid = str(uuid.uuid4())

    original_body = await request.body()

    public_model = None
    stream = False
    payload: dict[str, Any] | None = None

    content_type = request.headers.get(
        "content-type",
        "",
    ).lower()

    if (
        original_body
        and "application/json" in content_type
    ):
        try:
            candidate = json.loads(original_body)

            if isinstance(candidate, dict):
                payload = candidate

                if isinstance(
                    candidate.get("model"),
                    str,
                ):
                    public_model = candidate["model"]

                stream = bool(candidate.get("stream"))

        except Exception:
            pass

    payload_info = analyze_payload(
        payload,
        original_body,
    )

    forwarded_body = original_body

    if (
        payload is not None
        and stream
        and path in {
            "chat/completions",
            "responses",
        }
    ):
        modified_payload = inject_stream_usage(payload)
        forwarded_body = json.dumps(
            modified_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    query = request.url.query

    upstream_url = (
        f"{CORE_BASE_URL}/v1/"
        f"{path.lstrip('/')}"
    )

    if query:
        upstream_url += f"?{query}"

    headers = forward_request_headers(request)
    headers["x-log-request-id"] = request_uuid

    client = httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
    )

    usage: dict[str, Any] = {}
    finish_reason = None
    raw_error = None
    status_code = None
    response_bytes = 0
    ttft_ms: float | None = None

    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            headers=headers,
            content=forwarded_body,
        )

        response = await client.send(
            upstream_request,
            stream=True,
        )

        status_code = response.status_code

        response_content_type = response.headers.get(
            "content-type",
            "",
        ).lower()

        is_sse = (
            "text/event-stream"
            in response_content_type
            or stream
        )

        # ----------------------------------------------------
        # 上游错误
        # ----------------------------------------------------

        if status_code >= 400:
            data = await response.aread()
            response_bytes = len(data)

            if "json" in response_content_type:
                _, _, raw_error = parse_json_response(data)
            else:
                raw_error = data.decode(
                    "utf-8",
                    errors="replace",
                )[:5000]

            duration_ms = (
                time.perf_counter() - started_perf
            ) * 1000

            suspicion = analyze_suspicion(
                None,
                None,
                len(original_body),
                payload_info,
            )

            insert_log(
                make_log_row(
                    request_uuid,
                    request,
                    started_at,
                    finished_at=now_iso(),
                    public_model=public_model,
                    stream=stream,
                    status_code=status_code,
                    request_bytes=len(forwarded_body),
                    response_bytes=response_bytes,
                    client_body_bytes=len(original_body),
                    forwarded_body_bytes=len(forwarded_body),
                    uncached_prompt_tokens=suspicion[
                        "uncached_prompt_tokens"
                    ],
                    cache_ratio=suspicion[
                        "cache_ratio"
                    ],
                    token_byte_ratio=suspicion[
                        "token_byte_ratio"
                    ],
                    suspicion_score=suspicion[
                        "suspicion_score"
                    ],
                    suspicion_level=suspicion[
                        "suspicion_level"
                    ],
                    suspicion_flags=suspicion[
                        "suspicion_flags"
                    ],
                    duration_ms=duration_ms,
                    error=f"HTTP {status_code}",
                    raw_error_json=raw_error,
                    payload_structure_json=json.dumps(
                        payload_info,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    **payload_info,
                )
            )

            await response.aclose()
            await client.aclose()

            return Response(
                content=data,
                status_code=status_code,
                headers=forward_response_headers(
                    response.headers
                ),
                media_type=None,
            )

        # ----------------------------------------------------
        # 非流式
        # ----------------------------------------------------

        if not is_sse:
            data = await response.aread()
            response_bytes = len(data)

            usage, finish_reason, raw_error = parse_json_response(
                data
            )

            duration_ms = (
                time.perf_counter() - started_perf
            ) * 1000

            suspicion = analyze_suspicion(
                usage.get("prompt_tokens"),
                usage.get("cache_read_tokens"),
                len(original_body),
                payload_info,
            )

            output_rate, input_rate = calc_rates(
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                duration_ms,
                None,
            )

            insert_log(
                make_log_row(
                    request_uuid,
                    request,
                    started_at,
                    finished_at=now_iso(),
                    public_model=public_model,
                    stream=stream,
                    status_code=status_code,
                    request_bytes=len(forwarded_body),
                    response_bytes=response_bytes,
                    client_body_bytes=len(original_body),
                    forwarded_body_bytes=len(forwarded_body),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    reasoning_tokens=usage.get("reasoning_tokens"),
                    cached_tokens=usage.get("cached_tokens"),
                    cache_read_tokens=usage.get("cache_read_tokens"),
                    cache_creation_tokens=usage.get(
                        "cache_creation_tokens"
                    ),
                    uncached_prompt_tokens=suspicion[
                        "uncached_prompt_tokens"
                    ],
                    cache_ratio=suspicion["cache_ratio"],
                    token_byte_ratio=suspicion[
                        "token_byte_ratio"
                    ],
                    suspicion_score=suspicion[
                        "suspicion_score"
                    ],
                    suspicion_level=suspicion[
                        "suspicion_level"
                    ],
                    suspicion_flags=suspicion[
                        "suspicion_flags"
                    ],
                    duration_ms=duration_ms,
                    output_tokens_per_sec=output_rate,
                    input_tokens_per_sec=input_rate,
                    finish_reason=finish_reason,
                    raw_usage_json=usage.get(
                        "raw_usage_json"
                    ),
                    raw_error_json=raw_error,
                    payload_structure_json=json.dumps(
                        payload_info,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    **payload_info,
                )
            )

            await response.aclose()
            await client.aclose()

            return Response(
                content=data,
                status_code=status_code,
                headers=forward_response_headers(
                    response.headers
                ),
                media_type=None,
            )

        # ----------------------------------------------------
        # SSE / Streaming
        # ----------------------------------------------------

        response_headers = forward_response_headers(
            response.headers
        )

        async def iterator():
            nonlocal response_bytes
            nonlocal ttft_ms
            nonlocal usage
            nonlocal finish_reason
            nonlocal raw_error

            buffer = ""

            state: dict[str, Any] = {
                "usage": {},
                "finish_reason": None,
                "raw_error": None,
                "ttft_ms": None,
            }

            try:
                async for chunk in response.aiter_raw():
                    response_bytes += len(chunk)

                    text = chunk.decode(
                        "utf-8",
                        errors="replace",
                    )

                    buffer += (
                        text
                        .replace("\r\n", "\n")
                        .replace("\r", "\n")
                    )

                    while "\n\n" in buffer:
                        event, buffer = buffer.split(
                            "\n\n",
                            1,
                        )

                        merge_sse_event(
                            event,
                            state,
                            started_perf,
                        )

                    yield chunk

            except Exception as exc:
                raw_error = json.dumps(
                    {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
                raise

            finally:
                if buffer:
                    merge_sse_event(
                        buffer,
                        state,
                        started_perf,
                    )

                duration_ms = (
                    time.perf_counter() - started_perf
                ) * 1000

                if state.get("usage"):
                    usage = state["usage"]

                if state.get("finish_reason") is not None:
                    finish_reason = state["finish_reason"]

                if state.get("raw_error"):
                    raw_error = state["raw_error"]

                ttft_ms = state.get("ttft_ms")

                suspicion = analyze_suspicion(
                    usage.get("prompt_tokens"),
                    usage.get("cache_read_tokens"),
                    len(original_body),
                    payload_info,
                )

                output_rate, input_rate = calc_rates(
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    duration_ms,
                    ttft_ms,
                )

                insert_log(
                    make_log_row(
                        request_uuid,
                        request,
                        started_at,
                        finished_at=now_iso(),
                        public_model=public_model,
                        stream=True,
                        status_code=status_code,
                        request_bytes=len(forwarded_body),
                        response_bytes=response_bytes,
                        client_body_bytes=len(original_body),
                        forwarded_body_bytes=len(forwarded_body),
                        prompt_tokens=usage.get(
                            "prompt_tokens"
                        ),
                        completion_tokens=usage.get(
                            "completion_tokens"
                        ),
                        total_tokens=usage.get(
                            "total_tokens"
                        ),
                        reasoning_tokens=usage.get(
                            "reasoning_tokens"
                        ),
                        cached_tokens=usage.get(
                            "cached_tokens"
                        ),
                        cache_read_tokens=usage.get(
                            "cache_read_tokens"
                        ),
                        cache_creation_tokens=usage.get(
                            "cache_creation_tokens"
                        ),
                        uncached_prompt_tokens=suspicion[
                            "uncached_prompt_tokens"
                        ],
                        cache_ratio=suspicion[
                            "cache_ratio"
                        ],
                        token_byte_ratio=suspicion[
                            "token_byte_ratio"
                        ],
                        suspicion_score=suspicion[
                            "suspicion_score"
                        ],
                        suspicion_level=suspicion[
                            "suspicion_level"
                        ],
                        suspicion_flags=suspicion[
                            "suspicion_flags"
                        ],
                        ttft_ms=ttft_ms,
                        duration_ms=duration_ms,
                        output_tokens_per_sec=output_rate,
                        input_tokens_per_sec=input_rate,
                        finish_reason=finish_reason,
                        raw_usage_json=usage.get(
                            "raw_usage_json"
                        ),
                        raw_error_json=raw_error,
                        payload_structure_json=json.dumps(
                            payload_info,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        **payload_info,
                    )
                )

                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            iterator(),
            status_code=status_code,
            headers=response_headers,
            media_type=None,
        )

    # --------------------------------------------------------
    # Sidecar -> Core 连接失败
    # --------------------------------------------------------

    except httpx.RequestError as exc:
        duration_ms = (
            time.perf_counter() - started_perf
        ) * 1000

        suspicion = analyze_suspicion(
            None,
            None,
            len(original_body),
            payload_info,
        )

        insert_log(
            make_log_row(
                request_uuid,
                request,
                started_at,
                finished_at=now_iso(),
                public_model=public_model,
                stream=stream,
                status_code=502,
                request_bytes=len(forwarded_body),
                response_bytes=0,
                client_body_bytes=len(original_body),
                forwarded_body_bytes=len(forwarded_body),
                uncached_prompt_tokens=suspicion[
                    "uncached_prompt_tokens"
                ],
                cache_ratio=suspicion["cache_ratio"],
                token_byte_ratio=suspicion[
                    "token_byte_ratio"
                ],
                suspicion_score=suspicion[
                    "suspicion_score"
                ],
                suspicion_level=suspicion[
                    "suspicion_level"
                ],
                suspicion_flags=suspicion[
                    "suspicion_flags"
                ],
                duration_ms=duration_ms,
                error=(
                    "sidecar->core connection error: "
                    f"{exc}"
                ),
                payload_structure_json=json.dumps(
                    payload_info,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                **payload_info,
            )
        )

        await client.aclose()

        raise HTTPException(
            status_code=502,
            detail=f"连接核心 Gateway 失败: {exc}",
        ) from exc

    # --------------------------------------------------------
    # 其他异常
    # --------------------------------------------------------

    except Exception as exc:
        duration_ms = (
            time.perf_counter() - started_perf
        ) * 1000

        suspicion = analyze_suspicion(
            None,
            None,
            len(original_body),
            payload_info,
        )

        insert_log(
            make_log_row(
                request_uuid,
                request,
                started_at,
                finished_at=now_iso(),
                public_model=public_model,
                stream=stream,
                status_code=status_code or 500,
                request_bytes=len(forwarded_body),
                response_bytes=response_bytes,
                client_body_bytes=len(original_body),
                forwarded_body_bytes=len(forwarded_body),
                uncached_prompt_tokens=suspicion[
                    "uncached_prompt_tokens"
                ],
                cache_ratio=suspicion["cache_ratio"],
                token_byte_ratio=suspicion[
                    "token_byte_ratio"
                ],
                suspicion_score=suspicion[
                    "suspicion_score"
                ],
                suspicion_level=suspicion[
                    "suspicion_level"
                ],
                suspicion_flags=suspicion[
                    "suspicion_flags"
                ],
                duration_ms=duration_ms,
                error=str(exc),
                payload_structure_json=json.dumps(
                    payload_info,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                **payload_info,
            )
        )

        await client.aclose()
        raise


# ============================================================
# Health
# ============================================================

@app.get("/health", include_in_schema=False)
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "core": CORE_BASE_URL,
        "db": str(LOG_DB_PATH),
    }


@app.exception_handler(Exception)
async def generic_exception_handler(
    _: Request,
    exc: Exception,
) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
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