"""OpenRouter 模型价格查询站 —— 单文件后端（前后端分离）。

后端只提供 JSON API + 静态 HTML；前端为纯静态 HTML（内联 CSS/JS），
通过 fetch 调用 API，无 Jinja 模板渲染。

结构：
    app.py               —— 唯一后端文件（JSON API + 静态页面 + 后台刷新）
    static/*.html        —— 纯静态 HTML（内联 CSS + JS，前后端独立）
    db/openrouter/*.json —— 后端数据目录（models.json / config.json / cache.json）

数据策略：
- 每次请求实时读取本地 db/openrouter/cache.json。
- 若「当日数据尚未更新」，非阻塞地触发后台刷新，不阻塞首次打开页面。
- 所有路由挂在 Blueprint('openrouter', url_prefix='/openrouter') 下。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, Flask, jsonify, request, send_from_directory

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 后端数据目录（配置 + 缓存统一存放；相对项目根为 db/openrouter）
openrouter_DIR = os.path.join(_BASE_DIR, "db", "openrouter")
# 前端静态目录（纯静态 HTML，无 Jinja）
STATIC_DIR = os.path.join(_BASE_DIR, "static")
 
MODELS_FILE = os.path.join(openrouter_DIR, "models.json")
SETTINGS_FILE = os.path.join(openrouter_DIR, "config.json")
CACHE_FILE = os.path.join(openrouter_DIR, "cache.json")

API_URL = "https://openrouter.ai/api/v1/models"
TOKEN_TO_1M = 1_000_000  # 单个 token 单价 → 每百万 token 价格

# 默认关注模型（以 OpenRouter 官方 API 实际 id 为准）
DEFAULT_MODELS = [
    {"id": "deepseek/deepseek-v4-pro", "name": "DeepSeek V4 Pro", "enabled": True},
    {"id": "deepseek/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "enabled": True},
    {"id": "deepseek/deepseek-chat-v3.1", "name": "DeepSeek Chat V3.1", "enabled": True},
    {"id": "deepseek/deepseek-r1", "name": "DeepSeek R1", "enabled": True},
    {"id": "xiaomi/mimo-v2.5-pro", "name": "MiMo V2.5 Pro", "enabled": True},
    {"id": "xiaomi/mimo-v2.5", "name": "MiMo V2.5", "enabled": True},
]

DEFAULT_SETTINGS = {
    "usd_cny_rate": 7.2,
    "proxy": "",
    "refresh_interval_seconds": 86400,
    "manual_refresh_cooldown_seconds": 60,
    "auto_check_interval_seconds": 3600,
    "api_timeout_seconds": 15,
}

MAX_MODEL_ID_LEN = 200
MAX_MODEL_NAME_LEN = 100
AUTO_REFRESH_COOLDOWN = 60  # 请求触发自动刷新的冷却（秒）

# ---------------------------------------------------------------------------
# 锁
# ---------------------------------------------------------------------------
_io_lock = threading.RLock()      # 配置文件读写（可重入）
_cache_lock = threading.Lock()    # 缓存读写
_refresh_lock = threading.Lock()  # 防止后台/手动/请求触发刷新并发
_last_manual_refresh = 0.0
_last_auto_refresh_attempt = 0.0


# ---------------------------------------------------------------------------
# JSON 读写
# ---------------------------------------------------------------------------
def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 一般设置
# ---------------------------------------------------------------------------
def load_settings():
    with _io_lock:
        data = _read_json(SETTINGS_FILE, None)
        if not isinstance(data, dict):
            data = dict(DEFAULT_SETTINGS)
            _write_json_atomic(SETTINGS_FILE, data)
            return data
        changed = False
        for k, v in DEFAULT_SETTINGS.items():
            if k not in data:
                data[k] = v
                changed = True
        if changed:
            _write_json_atomic(SETTINGS_FILE, data)
        return data


def save_settings(settings):
    with _io_lock:
        current = load_settings()
        for k in DEFAULT_SETTINGS:
            if k in settings and settings[k] is not None:
                current[k] = settings[k]
        try:
            rate = float(current.get("usd_cny_rate", 7.2))
            if rate <= 0:
                rate = 7.2
        except (TypeError, ValueError):
            rate = 7.2
        current["usd_cny_rate"] = rate
        _write_json_atomic(SETTINGS_FILE, current)
        return current


# ---------------------------------------------------------------------------
# 模型关注配置
# ---------------------------------------------------------------------------
def _normalize_model(entry):
    mid = str(entry.get("id", "")).strip()
    name = str(entry.get("name", "")).strip() or mid
    enabled = bool(entry.get("enabled", True))
    return {"id": mid, "name": name, "enabled": enabled}


def load_models():
    with _io_lock:
        data = _read_json(MODELS_FILE, None)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            data = {"models": list(DEFAULT_MODELS)}
            _write_json_atomic(MODELS_FILE, data)
        models = [_normalize_model(m) for m in data.get("models", []) if isinstance(m, dict)]
        return [m for m in models if m["id"]]


def save_models(models):
    models = [_normalize_model(m) for m in models if isinstance(m, dict)]
    models = [m for m in models if m["id"]]
    with _io_lock:
        _write_json_atomic(MODELS_FILE, {"models": models})
    return models


def get_model_by_id(model_id):
    for m in load_models():
        if m["id"] == model_id:
            return m
    return None


def enabled_model_ids():
    return [m["id"] for m in load_models() if m["enabled"]]


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
_EMPTY_CACHE = {
    "updated_at": None,
    "source": "OpenRouter Official API",
    "last_error": None,
    "last_attempt_at": None,
    "models": {},
}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_cache():
    with _cache_lock:
        data = _read_json(CACHE_FILE, None)
        if not isinstance(data, dict):
            return dict(_EMPTY_CACHE, models={})
        for k, v in _EMPTY_CACHE.items():
            data.setdefault(k, v)
        if not isinstance(data.get("models"), dict):
            data["models"] = {}
        return data


def save_cache(cache):
    with _cache_lock:
        if not isinstance(cache, dict):
            cache = dict(_EMPTY_CACHE, models={})
        cache.setdefault("models", {})
        os.makedirs(openrouter_DIR, exist_ok=True)
        _write_json_atomic(CACHE_FILE, cache)


def is_stale_today(cache):
    """缓存是否「非当日」：updated_at 缺失/解析失败，或本地日期不是今天。"""
    updated = cache.get("updated_at")
    if not updated:
        return True
    try:
        dt = datetime.fromisoformat(updated)
    except (ValueError, TypeError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date() != datetime.now().date()


# ---------------------------------------------------------------------------
# OpenRouter API
# ---------------------------------------------------------------------------
class OpenRouterError(Exception):
    pass


def _build_session(settings):
    session = requests.Session()
    proxy = (settings.get("proxy") or "").strip() or os.environ.get("OPENROUTER_PROXY", "").strip()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    else:
        session.trust_env = False
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if api_key:
        session.headers.update({"Authorization": f"Bearer {api_key}"})
    session.headers.update({"Accept": "application/json"})
    return session


def _get(settings, url):
    timeout = int(settings.get("api_timeout_seconds", 15) or 15)
    session = _build_session(settings)
    try:
        resp = session.get(url, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise OpenRouterError(f"请求超时（>{timeout}s）") from exc
    except requests.exceptions.ConnectionError as exc:
        raise OpenRouterError(f"网络连接失败：{exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise OpenRouterError(f"请求异常：{exc}") from exc
    if resp.status_code != 200:
        raise OpenRouterError(f"OpenRouter API 返回 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter API 返回非 JSON 数据") from exc
    return payload


def _to_float_1m(value):
    """单个 token 单价（字符串/数字）→ USD / 1M tokens。"""
    if value is None:
        return None
    try:
        return round(float(value) * TOKEN_TO_1M, 6)
    except (TypeError, ValueError):
        return None


def _to_float(value):
    if value is None or value == "":
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def extract_model(raw):
    if not isinstance(raw, dict):
        return None
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None

    pricing_raw = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    context_length = raw.get("context_length")
    try:
        context_length = int(context_length) if context_length is not None else None
    except (TypeError, ValueError):
        context_length = None

    arch = raw.get("architecture")
    arch_dict = arch if isinstance(arch, dict) else {}
    if isinstance(arch, dict):
        arch_display = arch.get("modality") or arch.get("tokenizer") or ""
    elif isinstance(arch, str):
        arch_display = arch
    else:
        arch_display = ""

    supported_parameters = raw.get("supported_parameters")
    if not isinstance(supported_parameters, list):
        supported_parameters = []
    default_parameters = raw.get("default_parameters")
    if not isinstance(default_parameters, dict):
        default_parameters = {}
    top_provider = raw.get("top_provider")
    if not isinstance(top_provider, dict):
        top_provider = {}
    reasoning = raw.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}

    return {
        "id": model_id.strip(),
        "name": raw.get("name") if isinstance(raw.get("name"), str) else model_id.strip(),
        "input_price": _to_float_1m(pricing_raw.get("prompt")),
        "output_price": _to_float_1m(pricing_raw.get("completion")),
        "cache_read_price": _to_float_1m(pricing_raw.get("input_cache_read")),
        "raw_pricing": pricing_raw,
        "context_length": context_length,
        "architecture": arch_display,
        "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
        "created": raw.get("created"),
        "knowledge_cutoff": raw.get("knowledge_cutoff"),
        "supported_parameters": supported_parameters,
        "default_parameters": default_parameters,
        "top_provider": top_provider,
        "reasoning": reasoning,
        "modality": arch_dict.get("modality"),
        "input_modalities": arch_dict.get("input_modalities") if isinstance(arch_dict.get("input_modalities"), list) else [],
        "output_modalities": arch_dict.get("output_modalities") if isinstance(arch_dict.get("output_modalities"), list) else [],
        "tokenizer": arch_dict.get("tokenizer"),
        "instruct_type": arch_dict.get("instruct_type"),
        "per_request_limits": raw.get("per_request_limits"),
        "canonical_slug": raw.get("canonical_slug") if isinstance(raw.get("canonical_slug"), str) else None,
        "endpoints_url": (raw.get("links") or {}).get("details") if isinstance(raw.get("links"), dict) else None,
    }


def extract_provider(raw):
    if not isinstance(raw, dict):
        return None
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    context_length = raw.get("context_length")
    try:
        context_length = int(context_length) if context_length is not None else None
    except (TypeError, ValueError):
        context_length = None
    max_completion = raw.get("max_completion_tokens")
    try:
        max_completion = int(max_completion) if max_completion is not None else None
    except (TypeError, ValueError):
        max_completion = None
    name = raw.get("provider_name")
    if not isinstance(name, str) or not name:
        name = raw.get("name") or ""
    return {
        "name": name,
        "tag": raw.get("tag"),
        "quantization": raw.get("quantization"),
        "status": raw.get("status"),
        "input_price": _to_float_1m(pricing.get("prompt")),
        "output_price": _to_float_1m(pricing.get("completion")),
        "cache_read_price": _to_float_1m(pricing.get("input_cache_read")),
        "cache_write_price": _to_float_1m(pricing.get("input_cache_write")),
        "context_length": context_length,
        "max_completion_tokens": max_completion,
        "latency_ms": _to_float(raw.get("latency_last_30m")),
        "throughput_tps": _to_float(raw.get("throughput_last_30m")),
        "uptime_1d": _to_float(raw.get("uptime_last_1d")),
        "uptime_30m": _to_float(raw.get("uptime_last_30m")),
    }


def fetch_models(settings):
    payload = _get(settings, API_URL)
    if not isinstance(payload, dict):
        raise OpenRouterError("OpenRouter API 返回结构异常（非对象）")
    data = payload.get("data")
    if not isinstance(data, list):
        raise OpenRouterError("OpenRouter API 缺少 data 数组")
    return data


def fetch_watched_prices(model_ids, settings):
    data = fetch_models(settings)
    index = {}
    for item in data:
        parsed = extract_model(item)
        if parsed:
            index[parsed["id"]] = parsed
    prices = {}
    not_found = []
    for mid in model_ids:
        if mid in index:
            prices[mid] = index[mid]
        else:
            not_found.append(mid)
    return prices, not_found


def fetch_providers(model_slug, settings):
    """拉取某模型的逐 Provider 明细（endpoints 接口）。"""
    if not model_slug:
        return []
    if str(model_slug).startswith("http"):
        url = str(model_slug)
    elif str(model_slug).startswith("/"):
        url = "https://openrouter.ai" + str(model_slug)
    else:
        url = f"https://openrouter.ai/api/v1/models/{model_slug}/endpoints"

    payload = _get(settings, url)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    endpoints = data.get("endpoints")
    if not isinstance(endpoints, list):
        return []
    providers = []
    for e in endpoints:
        p = extract_provider(e)
        if p:
            providers.append(p)
    return providers


# ---------------------------------------------------------------------------
# 蓝图与状态
# ---------------------------------------------------------------------------
openrouter_bp = Blueprint("openrouter", __name__, url_prefix="/openrouter")


def _get_json():
    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def compute_status(cache, settings):
    if cache.get("updated_at"):
        return "updated" if not is_stale_today(cache) else "cached"
    if cache.get("last_error"):
        return "error"
    return "empty"


def build_models_response():
    config_models = load_models()
    cache = load_cache()
    settings = load_settings()
    cache_models = cache.get("models", {})

    items = []
    for cm in config_models:
        mid = cm["id"]
        entry = cache_models.get(mid)
        if isinstance(entry, dict) and entry.get("found"):
            items.append({
                "id": mid,
                "name": cm["name"] or entry.get("name", mid),
                "api_name": entry.get("name", mid),
                "enabled": cm["enabled"],
                "found": True,
                "input_price": entry.get("input_price"),
                "output_price": entry.get("output_price"),
                "cache_read_price": entry.get("cache_read_price"),
                "context_length": entry.get("context_length"),
                "architecture": entry.get("architecture", ""),
                "fetched_at": entry.get("fetched_at"),
            })
        else:
            items.append({
                "id": mid,
                "name": cm["name"],
                "api_name": None,
                "enabled": cm["enabled"],
                "found": False,
                "input_price": None,
                "output_price": None,
                "cache_read_price": None,
                "context_length": None,
                "architecture": "",
                "fetched_at": (entry or {}).get("fetched_at") if isinstance(entry, dict) else None,
            })

    return {
        "status": compute_status(cache, settings),
        "updated_at": cache.get("updated_at"),
        "source": cache.get("source"),
        "last_error": cache.get("last_error"),
        "usd_cny_rate": settings.get("usd_cny_rate"),
        "models": items,
    }


def do_refresh():
    settings = load_settings()
    ids = enabled_model_ids()
    cache = load_cache()

    if not ids:
        cache["last_attempt_at"] = now_iso()
        cache["last_error"] = "当前没有启用任何模型"
        save_cache(cache)
        return {"ok": False, "error": "当前没有启用任何模型"}

    try:
        prices, not_found = fetch_watched_prices(ids, settings)
    except OpenRouterError as exc:
        cache["last_attempt_at"] = now_iso()
        cache["last_error"] = str(exc)
        save_cache(cache)
        return {"ok": False, "error": str(exc)}

    now = now_iso()
    config_map = {m["id"]: m for m in load_models()}
    rebuilt = {}
    for mid in ids:
        if mid in prices:
            p = prices[mid]
            providers = []
            slug = p.get("endpoints_url") or p.get("canonical_slug")
            if slug:
                try:
                    providers = fetch_providers(slug, settings)
                except OpenRouterError:
                    providers = []
            rebuilt[mid] = {**p, "found": True, "fetched_at": now, "providers": providers}
        else:
            rebuilt[mid] = {
                "id": mid,
                "name": config_map.get(mid, {}).get("name", mid),
                "found": False,
                "input_price": None,
                "output_price": None,
                "cache_read_price": None,
                "raw_pricing": {},
                "context_length": None,
                "architecture": "",
                "providers": [],
                "fetched_at": now,
            }

    cache["models"] = rebuilt
    cache["updated_at"] = now
    cache["last_error"] = None
    cache["last_attempt_at"] = now
    cache["source"] = "OpenRouter Official API"
    save_cache(cache)

    return {"ok": True, "updated_at": now, "found": len(prices), "not_found": len(not_found)}


# ---------------------------------------------------------------------------
# 页面路由（纯静态 HTML，前后端分离；前端自行调用下方 JSON API）
# ---------------------------------------------------------------------------
@openrouter_bp.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@openrouter_bp.route("/settings")
def settings():
    return send_from_directory(STATIC_DIR, "settings.html")


@openrouter_bp.route("/model/<path:model_id>")
def model_detail(model_id):
    # model_id 仅用于匹配 URL；前端从 window.location.pathname 自行解析
    return send_from_directory(STATIC_DIR, "detail.html")


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------
@openrouter_bp.route("/api/models", methods=["GET"])
def api_list_models():
    return jsonify(build_models_response())


@openrouter_bp.route("/api/models/<path:model_id>", methods=["GET"])
def api_model_detail(model_id):
    cache = load_cache()
    settings = load_settings()
    config_model = get_model_by_id(model_id)
    entry = cache.get("models", {}).get(model_id)

    if config_model is None and not isinstance(entry, dict):
        return jsonify({"error": "模型不存在"}), 404

    found = bool(isinstance(entry, dict) and entry.get("found"))
    config_name = config_model.get("name") if config_model else None
    api_name = entry.get("name") if isinstance(entry, dict) else None

    return jsonify({
        "id": model_id,
        "name": config_name or api_name or model_id,
        "api_name": api_name,
        "enabled": config_model.get("enabled") if config_model else None,
        "found": found,
        "status": compute_status(cache, settings),
        "updated_at": cache.get("updated_at"),
        "fetched_at": entry.get("fetched_at") if isinstance(entry, dict) else None,
        "source": cache.get("source"),
        "last_error": cache.get("last_error"),
        "usd_cny_rate": settings.get("usd_cny_rate"),
        "model": entry if isinstance(entry, dict) else {},
    })


@openrouter_bp.route("/api/models", methods=["POST"])
def api_add_model():
    data = _get_json()
    model_id = str(data.get("id", "")).strip()
    name = str(data.get("name", "")).strip()
    enabled = bool(data.get("enabled", True))

    if not model_id:
        return jsonify({"error": "Model ID 不能为空"}), 400
    if len(model_id) > MAX_MODEL_ID_LEN:
        return jsonify({"error": f"Model ID 过长（>{MAX_MODEL_ID_LEN} 字符）"}), 400
    if len(name) > MAX_MODEL_NAME_LEN:
        return jsonify({"error": f"显示名称过长（>{MAX_MODEL_NAME_LEN} 字符）"}), 400

    models = load_models()
    if any(m["id"] == model_id for m in models):
        return jsonify({"error": "该 Model ID 已存在"}), 409

    models.append({"id": model_id, "name": name or model_id, "enabled": enabled})
    save_models(models)
    return jsonify({"ok": True, "models": load_models()})


@openrouter_bp.route("/api/models/<path:model_id>", methods=["PUT"])
def api_update_model(model_id):
    data = _get_json()
    models = load_models()
    target = next((m for m in models if m["id"] == model_id), None)
    if target is None:
        return jsonify({"error": "模型不存在"}), 404

    if "name" in data:
        name = str(data.get("name", "")).strip()
        if len(name) > MAX_MODEL_NAME_LEN:
            return jsonify({"error": f"显示名称过长（>{MAX_MODEL_NAME_LEN} 字符）"}), 400
        if name:
            target["name"] = name
    if "enabled" in data:
        target["enabled"] = bool(data.get("enabled"))

    save_models(models)
    return jsonify({"ok": True, "models": load_models()})


@openrouter_bp.route("/api/models/<path:model_id>", methods=["DELETE"])
def api_delete_model(model_id):
    models = load_models()
    new_models = [m for m in models if m["id"] != model_id]
    if len(new_models) == len(models):
        return jsonify({"error": "模型不存在"}), 404
    save_models(new_models)
    return jsonify({"ok": True, "models": load_models()})


@openrouter_bp.route("/api/refresh", methods=["POST"])
def api_refresh():
    global _last_manual_refresh
    settings = load_settings()
    cooldown = int(settings.get("manual_refresh_cooldown_seconds", 60) or 60)

    with _refresh_lock:
        now = time.time()
        if now - _last_manual_refresh < cooldown:
            return jsonify({"error": "刷新过于频繁，请稍后再试。"}), 429
        _last_manual_refresh = now
        result = do_refresh()

    if result.get("ok"):
        return jsonify({"ok": True, **result, **build_models_response()})
    return jsonify({"ok": False, "error": result.get("error", "刷新失败")}), 502


@openrouter_bp.route("/api/status", methods=["GET"])
def api_status():
    settings = load_settings()
    cache = load_cache()
    return jsonify({
        "status": compute_status(cache, settings),
        "updated_at": cache.get("updated_at"),
        "last_attempt_at": cache.get("last_attempt_at"),
        "last_error": cache.get("last_error"),
        "source": cache.get("source"),
        "usd_cny_rate": settings.get("usd_cny_rate"),
        "refresh_interval_seconds": settings.get("refresh_interval_seconds"),
        "manual_refresh_cooldown_seconds": settings.get("manual_refresh_cooldown_seconds"),
    })


@openrouter_bp.route("/api/config", methods=["POST"])
def api_config():
    data = _get_json()
    allowed = set(DEFAULT_SETTINGS.keys())
    update = {k: data[k] for k in allowed if k in data}
    try:
        settings = save_settings(update)
    except Exception as exc:
        return jsonify({"error": f"保存设置失败：{exc}"}), 500
    return jsonify({"ok": True, "settings": settings})


# ---------------------------------------------------------------------------
# 后台自动更新
# ---------------------------------------------------------------------------
def _auto_refresh_if_needed():
    try:
        cache = load_cache()
        if is_stale_today(cache):
            with _refresh_lock:
                do_refresh()
    except Exception:
        pass


def trigger_auto_refresh_if_stale():
    global _last_auto_refresh_attempt
    try:
        cache = load_cache()
        if not is_stale_today(cache):
            return False
    except Exception:
        return False

    if time.time() - _last_auto_refresh_attempt < AUTO_REFRESH_COOLDOWN:
        return False
    if not _refresh_lock.acquire(blocking=False):
        return False

    _last_auto_refresh_attempt = time.time()

    def _worker():
        try:
            do_refresh()
        finally:
            _refresh_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return True


@openrouter_bp.before_request
def _auto_refresh_hook():
    trigger_auto_refresh_if_stale()


def start_background_refresh():
    def loop():
        _auto_refresh_if_needed()
        while True:
            try:
                interval = int(load_settings().get("auto_check_interval_seconds", 3600) or 3600)
            except Exception:
                interval = 3600
            time.sleep(interval)
            _auto_refresh_if_needed()

    threading.Thread(target=loop, name="openrouter-auto-refresh", daemon=True).start()


# ---------------------------------------------------------------------------
# 应用与入口
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False
app.register_blueprint(openrouter_bp)


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_background_refresh()

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
