# -*- coding: utf-8 -*-
"""
WomenPic 蓝图模块（可被主 Flask 应用挂载）
===========================================
包含：
  - 多渠道路由的女星数据接口（原有功能）
  - 翻译接口（POST /womenpic/api/translate）
  - SQLite 翻译缓存数据库（自动初始化）

使用方法：
  from womenpic_blueprint import womenpic_bp
  app.register_blueprint(womenpic_bp)

环境变量：
  OP_API_KEY            API Key（用于标题翻译）
  MODEL_OP_URL          翻译API地址，默认 https://openrouter.ai/api/v1/chat/completions
  OP_TRANSLATE_MODEL    使用的模型，默认 deepseek/deepseek-chat
  OP_DISABLE_REASONING  设为 1 可禁用推理模型的思考过程
  OPENROUTER_REFERER    可选，站点 URL（推荐设置）
  OPENROUTER_TITLE      可选，应用名称（推荐设置）
  TRANSLATE_DEBUG       设为 1 可开启翻译调式日志
"""

import io
import json
import os
import sqlite3
import zipfile
import time
import requests
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, send_from_directory, abort, current_app

# ──────────────────────────────────────────────
# 【集中配置区域】修改参数只需改动此处
# ──────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent  # 项目根目录
WOMENS_DIR = ROOT / "db" / "womenpic"
DB_PATH = WOMENS_DIR / "womenpic.db"
ZIP_PATH = WOMENS_DIR / "images.zip"
TRANSLATIONS_DB_PATH = WOMENS_DIR / "translations.db"  # 翻译缓存数据库

# 渠道目录配置
CHANNELS = {
    "default": WOMENS_DIR / "default",
    "jav": WOMENS_DIR / "jav",
}
LEGACY_DIR = WOMENS_DIR

# ── 翻译接口配置 ──
API_KEY = os.environ.get("OP_API_KEY", "")
# 注意：此处应使用 os.environ.get("OP_TRANSLATE_MODEL", "默认模型")
TRANSLATE_MODEL = 'deepseek/deepseek-v4-flash'
OPENROUTER_API_URL = os.environ.get("MODEL_OP_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_REFERER = os.environ.get("OPENROUTER_REFERER", "")
OPENROUTER_TITLE = os.environ.get("OPENROUTER_TITLE", "WomenPic")
DISABLE_REASONING = os.environ.get("OP_DISABLE_REASONING", "0") == "1"

# ── 调试开关 ──
TRANSLATE_DEBUG = os.environ.get("TRANSLATE_DEBUG", "0") == "1"

# ──────────────────────────────────────────────
# 内部工具函数
# ──────────────────────────────────────────────
_db_conn: sqlite3.Connection | None = None
_zip_file: zipfile.ZipFile | None = None
_trans_db: sqlite3.Connection | None = None
_use_sqlite = False


def _log(msg):
    """条件日志输出，仅在调试模式下生效。"""
    if TRANSLATE_DEBUG and current_app:
        current_app.logger.debug(f"[WomenPic翻译] {msg}")


def _get_db() -> sqlite3.Connection | None:
    global _db_conn
    if _db_conn is None and DB_PATH.exists():
        _db_conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
    return _db_conn


def _get_zip() -> zipfile.ZipFile | None:
    global _zip_file
    if _zip_file is None and ZIP_PATH.exists():
        _zip_file = zipfile.ZipFile(str(ZIP_PATH), "r")
    return _zip_file


def _get_translation_db() -> sqlite3.Connection:
    global _trans_db
    if _trans_db is None:
        TRANSLATIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _trans_db = sqlite3.connect(str(TRANSLATIONS_DB_PATH), check_same_thread=False)
        _trans_db.row_factory = sqlite3.Row
        _trans_db.execute(
            """CREATE TABLE IF NOT EXISTS translations (
                key TEXT PRIMARY KEY,
                translation TEXT NOT NULL,
                model TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        _trans_db.commit()
    return _trans_db


def _init_mode():
    global _use_sqlite
    _use_sqlite = DB_PATH.exists()
    if _use_sqlite:
        _get_db()
        _get_zip()
    if current_app and current_app.debug:
        mode_str = "SQLite + ZIP" if _use_sqlite else "文件夹"
        current_app.logger.info(f"WomenPic 数据模式: {mode_str}")


def _safe_read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _get_channel_dirs() -> list[tuple[str, Path]]:
    dirs = []
    for ch, path in CHANNELS.items():
        if path.exists() and path.is_dir():
            dirs.append((ch, path))
    if LEGACY_DIR.exists() and not dirs:
        has_data = any(
            d.is_dir() and not d.name.startswith(".") and d.name not in CHANNELS
            for d in LEGACY_DIR.iterdir()
        )
        if has_data:
            dirs.append(("default", LEGACY_DIR))
    return dirs


def _scan_all_names() -> list[dict]:
    names_map = {}
    db = _get_db()
    if db:
        rows = db.execute(
            "SELECT name, COALESCE(channel, 'default') as channel FROM actresses ORDER BY name COLLATE NOCASE"
        ).fetchall()
        for r in rows:
            names_map[r["name"]] = r["channel"]
    for ch, ch_dir in _get_channel_dirs():
        for d in ch_dir.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                if d.name not in names_map:
                    names_map[d.name] = ch
    return [{"name": n, "channel": c} for n, c in sorted(names_map.items(), key=lambda x: x[0].lower())]


def _find_star_dir(name: str) -> tuple[str, Path] | None:
    db = _get_db()
    if db:
        row = db.execute(
            "SELECT COALESCE(channel, 'default') as channel FROM actresses WHERE name = ?",
            (name,),
        ).fetchone()
        if row:
            ch = row["channel"]
            ch_dir = CHANNELS.get(ch, LEGACY_DIR)
            return (ch, ch_dir / name)
    for ch, ch_dir in _get_channel_dirs():
        star_dir = ch_dir / name
        if star_dir.exists():
            return (ch, star_dir)
    return None


def _source(name: str) -> str:
    db = _get_db()
    if db:
        row = db.execute("SELECT 1 FROM actresses WHERE name = ?", (name,)).fetchone()
        if row:
            return "sqlite"
    found = _find_star_dir(name)
    return "folder" if found else "none"


# ──────────────────────────────────────────────
# 数据构建函数（原有功能，保持不变）
# ──────────────────────────────────────────────
def _build_summary(name: str) -> dict:
    src = _source(name)
    found = _find_star_dir(name)
    channel = found[0] if found else "default"

    if src == "sqlite":
        db = _get_db()
        row = db.execute(
            "SELECT name, tmdb_url, birthday, birthplace, summary, profile_image, "
            "image_count, works_count, COALESCE(channel, 'default') as channel "
            "FROM actresses WHERE name = ?",
            (name,),
        ).fetchone()
        if row:
            profile_url = f"/womenpic/images/{row['channel']}/{name}/{row['profile_image']}" if row["profile_image"] else None
            return {
                "name": row["name"],
                "channel": row["channel"],
                "profile_image": profile_url,
                "image_count": row["image_count"] or 0,
                "works_count": row["works_count"] or 0,
                "birthday": row["birthday"] or "",
                "birthplace": row["birthplace"] or "",
                "summary": row["summary"] or "",
                "tmdb_url": row["tmdb_url"] or "",
            }

    if not found:
        return {
            "name": name, "channel": channel,
            "profile_image": None, "image_count": 0, "works_count": 0,
            "birthday": "", "birthplace": "", "summary": "", "tmdb_url": "",
        }

    ch, folder = found
    info = _safe_read_json(folder / "info.json") or {}
    core = info.get("core", {})
    body = info.get("body", {})
    baike = _safe_read_json(folder / "baike.json") or {}
    file_channel = info.get("channel", ch)

    profile_img = info.get("headshot", "") or info.get("downloaded_profile", "")
    images = info.get("images", [])
    profile_url = None
    if profile_img:
        profile_url = f"/womenpic/images/{file_channel}/{name}/{profile_img}"
    elif images:
        first_img = images[0]
        fname = first_img.get("filename", "") or first_img.get("file", "")
        if fname:
            profile_url = f"/womenpic/images/{file_channel}/{name}/{fname}"

    img_dir = folder / "images"
    img_count = len(list(img_dir.glob("*"))) if img_dir.exists() else 0

    works = info.get("works", info.get("filmography", []))
    works_count = len(works) if isinstance(works, list) else 0

    baike_summary = baike.get("summary", "")
    if len(baike_summary) > 120:
        baike_summary = baike_summary[:120] + "…"

    return {
        "name": name,
        "channel": file_channel,
        "profile_image": profile_url,
        "image_count": img_count,
        "works_count": works_count,
        "birthday": core.get("birthday") or info.get("birthday", baike.get("birthday", "")),
        "birthplace": core.get("birthplace") or info.get("birthplace", baike.get("birthplace", "")),
        "height_cm": body.get("height_cm") or "",
        "bust_cm": body.get("bust_cm") or "",
        "waist_cm": body.get("waist_cm") or "",
        "hip_cm": body.get("hip_cm") or "",
        "cup": body.get("cup") or "",
        "age": core.get("age"),
        "summary": baike_summary or (
            (info.get("biography", "")[:120] + "…")
            if len(info.get("biography", "")) > 120
            else info.get("biography", "")
        ),
        "tmdb_url": info.get("tmdb_url", ""),
    }


def _build_detail(name: str) -> dict:
    src = _source(name)
    found = _find_star_dir(name)
    channel = found[0] if found else "default"

    if src == "sqlite":
        db = _get_db()
        row = db.execute(
            "SELECT * FROM actresses WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            abort(404)

        file_channel = row["channel"] or "default"

        baike_rows = db.execute("SELECT key, value FROM baike WHERE name = ?", (name,)).fetchall()
        baike = {r["key"]: r["value"] for r in baike_rows}

        img_rows = db.execute(
            "SELECT filename, original_url, size_kb FROM images WHERE name = ? ORDER BY filename",
            (name,)
        ).fetchall()
        img_list = [{
            "filename": r["filename"],
            "url": f"/womenpic/images/{file_channel}/{name}/{r['filename']}",
            "size_kb": r["size_kb"],
        } for r in img_rows]

        work_rows = db.execute(
            "SELECT year, title, role, url, COALESCE(code,'') as code, "
            "COALESCE(date,'') as date, COALESCE(tags,'[]') as tags FROM filmography "
            "WHERE name = ? ORDER BY date DESC, id ASC",
            (name,)
        ).fetchall()
        works = [{
            "year": r["year"], "title": r["title"], "role": r["role"], "url": r["url"],
            "code": r["code"] or "", "date": r["date"] or "",
            "tags": json.loads(r["tags"]) if r["tags"] else [],
        } for r in work_rows]

        return {
            "name": row["name"],
            "channel": file_channel,
            "tmdb_id": row["tmdb_id"] or "",
            "tmdb_url": row["tmdb_url"] or "",
            "birthday": row["birthday"] or "",
            "birthplace": row["birthplace"] or "",
            "biography": row["biography"] or "",
            "baike": baike,
            "wiki_zh": row["wiki_zh"] or "",
            "wiki_en": row["wiki_en"] or "",
            "images": img_list,
            "filmography": works[:100],
            "filmography_total": len(works),
            "info": {},
        }

    if not found:
        abort(404)

    ch, folder = found
    info = _safe_read_json(folder / "info.json") or {}
    core = info.get("core", {})
    body = info.get("body", {})
    baike = _safe_read_json(folder / "baike.json") or {}
    file_channel = info.get("channel", ch)

    img_dir = folder / "images"
    img_list = []
    if img_dir.exists():
        for f in sorted(img_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                img_list.append({
                    "filename": f.name,
                    "url": f"/womenpic/images/{file_channel}/{name}/{f.name}",
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })

    works = info.get("works", [])
    if not works:
        works = info.get("filmography", [])
    if not works and (folder / "filmography.json").exists():
        works = _safe_read_json(folder / "filmography.json") or []

    wiki_zh = (folder / "wiki_zh.txt").read_text(encoding="utf-8")[:2000] if (folder / "wiki_zh.txt").exists() else ""
    wiki_en = (folder / "wiki_en.txt").read_text(encoding="utf-8")[:2000] if (folder / "wiki_en.txt").exists() else ""

    baike_from_jav = {
        "name_cn": core.get("name_cn", ""),
        "name_en": core.get("name_en", ""),
        "birthday": core.get("birthday", ""),
        "age": core.get("age"),
        "birthplace": core.get("birthplace", ""),
        "nationality": core.get("nationality", ""),
        "occupation": core.get("occupation", ""),
        "bodyHeight": str(body.get("height_cm", "")),
        "bodyWeight": str(body.get("weight_kg", "")),
        "bust": str(body.get("bust_cm", "")),
        "waist": str(body.get("waist_cm", "")),
        "hip": str(body.get("hip_cm", "")),
        "cup": body.get("cup", ""),
        "aliases": core.get("aliases", ""),
        "biography": info.get("biography", core.get("biography", "")),
    }
    merged_baike = {**baike_from_jav, **baike}

    return {
        "name": name,
        "channel": file_channel,
        "tmdb_id": info.get("tmdb_id", ""),
        "tmdb_url": info.get("tmdb_url", ""),
        "birthday": core.get("birthday") or info.get("birthday", baike.get("birthday", "")),
        "birthplace": core.get("birthplace") or info.get("birthplace", baike.get("birthplace", "")),
        "biography": info.get("biography", core.get("biography", "")),
        "baike": merged_baike,
        "wiki_zh": wiki_zh,
        "wiki_en": wiki_en,
        "images": img_list,
        "filmography": works[:100],
        "filmography_total": len(works),
        "info": {
            k: v for k, v in info.items()
            if k not in ("filmography", "images", "biography", "baidu_baike", "works")
        },
    }


def _serve_image(channel: str, name: str, filename: str):
    zf = _get_zip()
    if zf:
        zpath = f"{channel}/{name}/{filename}"
        try:
            data = zf.read(zpath)
            return send_file(io.BytesIO(data), mimetype="image/jpeg")
        except KeyError:
            pass
        try:
            data = zf.read(f"{name}/{filename}")
            return send_file(io.BytesIO(data), mimetype="image/jpeg")
        except KeyError:
            pass

    ch_dir = CHANNELS.get(channel, LEGACY_DIR)
    img_path = ch_dir / name / "images" / filename
    if img_path.exists():
        return send_file(str(img_path.resolve()), mimetype="image/jpeg")

    img_path = LEGACY_DIR / name / "images" / filename
    if img_path.exists():
        return send_file(str(img_path.resolve()), mimetype="image/jpeg")

    abort(404)


# ──────────────────────────────────────────────
# 翻译逻辑（已去除 print，改用 _log）
# ──────────────────────────────────────────────
def _translate_via_openrouter(titles: list[str], source_lang: str = "ja", target_lang: str = "zh") -> list[str]:
    """使用兼容 OpenAI 的接口（如 OpenRouter）批量翻译。"""
    if not API_KEY:
        _log("未设置 OP_API_KEY，跳过翻译")
        return titles

    model = TRANSLATE_MODEL
    if not model:
        model = "deepseek/deepseek-chat"
        _log(f"未设置 OP_TRANSLATE_MODEL，使用默认模型: {model}")

    prompt = (
        "你是日文成人向标题翻译模型。\n"
        "将输入日文标题翻译为自然中文标题。\n"
        "要求：\n"
        "只输出中文标题\n"
        "不解释、不分析、不加引号\n"
        "保留人名、女优名、作品名\n"
        "理解成人向固定词汇和场景表达\n"
        "优化为中文资源站常用标题风格\n"
        "避免机械直译和日式语序\n"
        "保持原标题信息量，不扩写\n"
        "多个标题换行输出\n"
        "标题：\n"
    )
    for i, t in enumerate(titles, 1):
        prompt += f"{i}. {t}\n"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENROUTER_REFERER:
        headers["HTTP-Referer"] = OPENROUTER_REFERER
    if OPENROUTER_TITLE:
        headers["X-Title"] = OPENROUTER_TITLE

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个专业日译中翻译助手。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
        "stream": False,
    }
    if DISABLE_REASONING:
        payload["reasoning"] = {"max_tokens": 0}

    try:
        _log(f"请求地址: {OPENROUTER_API_URL}")
        _log(f"模型: {model}")
        resp = requests.post(OPENROUTER_API_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()

        content = None
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass

        if content is None and "error" in result:
            _log(f"接口返回错误: {result['error']}")
            return titles

        if content is None:
            _log("content 为空，尝试从 reasoning 中提取...")
            try:
                reasoning = result["choices"][0]["message"]["reasoning"]
                if reasoning:
                    lines = [l.strip() for l in reasoning.split("\n") if l.strip()]
                    content = "\n".join(lines[-len(titles):])
            except Exception:
                pass

        if content is None:
            _log("无法从响应中提取 content，返回原文")
            return titles

        lines = [line.strip() for line in content.split("\n") if line.strip()]
        cleaned = []
        for line in lines:
            if len(line) > 2 and line[0].isdigit() and (". " in line[:4] or ") " in line[:4]):
                line = line.split(". ", 1)[-1].split(") ", 1)[-1]
            cleaned.append(line)

        if len(cleaned) < len(titles):
            _log(f"返回行数不足({len(cleaned)}<{len(titles)})，用原文补全")
            cleaned += titles[len(cleaned):]
        return cleaned[:len(titles)]

    except requests.exceptions.RequestException as e:
        _log(f"网络请求失败: {e}")
        return titles
    except Exception as e:
        _log(f"未预期的错误: {e}")
        return titles


def _batch_translate(texts: list[str], source_lang: str = "auto", target_lang: str = "zh") -> list[str]:
    if not texts:
        return []
    db = _get_translation_db()
    cache_map = {}
    uncached = []
    for t in texts:
        row = db.execute("SELECT translation FROM translations WHERE key = ?", (t,)).fetchone()
        if row:
            cache_map[t] = row["translation"]
        else:
            uncached.append(t)

    _log(f"缓存命中 {len(cache_map)} 条，需翻译 {len(uncached)} 条")

    if uncached:
        translated = _translate_via_openrouter(uncached, source_lang, target_lang)
        # 获取实际使用的模型名（非空）
        actual_model = TRANSLATE_MODEL or "deepseek/deepseek-chat"
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for orig, trans in zip(uncached, translated):
            if trans != orig:
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO translations (key, translation, model, created_at) VALUES (?, ?, ?, ?)",
                        (orig, trans, actual_model, now)
                    )
                except Exception as e:
                    _log(f"缓存写入失败 {orig}: {e}")
                cache_map[orig] = trans
            else:
                cache_map[orig] = orig
        db.commit()
    return [cache_map.get(t, t) for t in texts]


# ──────────────────────────────────────────────
# 蓝图路由定义
# ──────────────────────────────────────────────
womenpic_bp = Blueprint("womenpic", __name__, url_prefix="/womenpic")

_init_mode()


@womenpic_bp.route("/api/actresses")
def api_actresses():
    channel_filter = request.args.get("channel", "").strip()
    all_items = _scan_all_names()
    if channel_filter:
        all_items = [x for x in all_items if x["channel"] == channel_filter]
    items = [_build_summary(x["name"]) for x in all_items]
    channels_count = {}
    for x in all_items:
        ch = x["channel"]
        channels_count[ch] = channels_count.get(ch, 0) + 1
    return jsonify({
        "count": len(items),
        "data": items,
        "channels": channels_count,
        "mode": "sqlite" if _use_sqlite else "folder",
    })


@womenpic_bp.route("/api/channels")
def api_channels():
    all_items = _scan_all_names()
    channels_count = {}
    for x in all_items:
        ch = x["channel"]
        channels_count[ch] = channels_count.get(ch, 0) + 1
    channel_labels = {
        "default": "全部女星",
        "jav": "Jav 日本女星",
    }
    return jsonify({
        "channels": [
            {"id": k, "label": channel_labels.get(k, k), "count": v}
            for k, v in sorted(channels_count.items())
        ],
        "total": len(all_items),
    })


@womenpic_bp.route("/api/actress/<path:name>")
def api_actress_detail(name):
    return jsonify(_build_detail(name))


@womenpic_bp.route("/images/<path:channel>/<path:name>/<path:filename>")
def serve_image_channel(channel, name, filename):
    return _serve_image(channel, name, filename)


@womenpic_bp.route("/images/<path:name>/<path:filename>")
def serve_image_legacy(name, filename):
    return _serve_image("default", name, filename)


@womenpic_bp.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    channel_filter = request.args.get("channel", "").strip()
    if not q:
        return jsonify({"count": 0, "data": []})
    all_items = _scan_all_names()
    if channel_filter:
        all_items = [x for x in all_items if x["channel"] == channel_filter]
    matched = [x for x in all_items if q.lower() in x["name"].lower()]
    items = [_build_summary(x["name"]) for x in matched]
    return jsonify({"count": len(items), "data": items})


@womenpic_bp.route("/api/translate", methods=["POST"])
def api_translate():
    if not request.is_json:
        return jsonify({"error": "请求必须为JSON"}), 400
    data = request.get_json()
    texts = data.get("texts", [])
    if not isinstance(texts, list) or not texts:
        return jsonify({"error": "缺少texts参数"}), 400
    source_lang = data.get("source_lang", "auto")
    target_lang = data.get("target_lang", "zh")
    try:
        translations = _batch_translate(texts, source_lang, target_lang)
        return jsonify({"translations": translations})
    except Exception as e:
        return jsonify({"error": str(e)}), 500