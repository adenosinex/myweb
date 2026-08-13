# -*- coding: utf-8 -*-
"""
WomenPic 蓝图模块（完整版：翻译缓存 + 搜索加速 + 自动翻译）
=============================================================
路径：
  db/womenpic/womenpic.db       原始数据（只读）
  db/womenpic/images.zip        图片包
  db/womenpic/translations.db   翻译缓存（key-value）
  db/womenpic/works_search.db   作品缓存与搜索加速（可重建）

使用方法：
  from womenpic_blueprint import womenpic_bp
  app.register_blueprint(womenpic_bp)

环境变量：
  OP_API_KEY            API Key（用于标题翻译）
  MODEL_OP_URL          翻译API地址，默认 https://openrouter.ai/api/v1/chat/completions
  OP_TRANSLATE_MODEL    使用的模型，默认 deepseek/deepseek-chat
  OP_DISABLE_REASONING  设为 1 可禁用推理模型的思考过程
  OPENROUTER_REFERER    可选，站点 URL
  OPENROUTER_TITLE      可选，应用名称
  TRANSLATE_DEBUG       设为 1 开启翻译调试日志
"""

import io
import json
import os
import sqlite3
import zipfile
import time
import threading
import requests
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, abort, current_app

# ──────────────────────────────────────────────
# 【集中配置区域】
# ──────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
WOMENS_DIR = ROOT / "db" / "womenpic"
DB_PATH = WOMENS_DIR / "womenpic.db"            # 原始数据
ZIP_PATH = WOMENS_DIR / "images.zip"            # 图片包
TRANSLATIONS_DB_PATH = WOMENS_DIR / "translations.db"   # 翻译缓存
WORKS_CACHE_DB_PATH = WOMENS_DIR / "works_search.db"    # 搜索加速（可选）

CHANNELS = {
    "default": WOMENS_DIR / "default",
    "jav": WOMENS_DIR / "jav",
}
LEGACY_DIR = WOMENS_DIR

# ── 翻译接口配置 ──
API_KEY = os.environ.get("OP_API_KEY", "")
TRANSLATE_MODEL = os.environ.get("OP_TRANSLATE_MODEL", "deepseek/deepseek-chat")
OPENROUTER_API_URL = os.environ.get("MODEL_OP_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_REFERER = os.environ.get("OPENROUTER_REFERER", "")
OPENROUTER_TITLE = os.environ.get("OPENROUTER_TITLE", "WomenPic")
DISABLE_REASONING = os.environ.get("OP_DISABLE_REASONING", "0") == "1"
TRANSLATE_DEBUG = os.environ.get("TRANSLATE_DEBUG", "0") == "1"

# ── 全局缓存 ──
_db_conn: sqlite3.Connection | None = None
_zip_file: zipfile.ZipFile | None = None
_trans_db: sqlite3.Connection | None = None
_works_cache_db: sqlite3.Connection | None = None
_use_sqlite = False

def _log(msg):
    if TRANSLATE_DEBUG and current_app:
        current_app.logger.debug(f"[WomenPic] {msg}")

# ──────────────────────────────────────────────
# 数据库连接
# ──────────────────────────────────────────────
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

def _get_works_cache_db() -> sqlite3.Connection:
    global _works_cache_db
    if _works_cache_db is None:
        WORKS_CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _works_cache_db = sqlite3.connect(str(WORKS_CACHE_DB_PATH), check_same_thread=False)
        _works_cache_db.row_factory = sqlite3.Row
        _init_works_cache_tables(_works_cache_db)
    return _works_cache_db

def _init_works_cache_tables(db):
    """创建作品缓存表（若不存在）"""
    db.execute("""CREATE TABLE IF NOT EXISTS actress_info (
        name TEXT PRIMARY KEY,
        name_zh TEXT,
        channel TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS works (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actress_name TEXT NOT NULL,
        title TEXT NOT NULL,
        title_zh TEXT,
        code TEXT,
        date TEXT,
        url TEXT,
        tags TEXT,
        FOREIGN KEY (actress_name) REFERENCES actress_info(name)
    )""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_works_unique 
        ON works(actress_name, title, code, date)""")
    db.commit()

def _init_mode():
    global _use_sqlite
    _use_sqlite = DB_PATH.exists()
    if _use_sqlite:
        _get_db()
        _get_zip()
    if current_app and current_app.debug:
        current_app.logger.info(f"WomenPic 数据模式: {'SQLite+ZIP' if _use_sqlite else '文件夹'}")

# ──────────────────────────────────────────────
# 原有数据读取函数（保持不变）
# ──────────────────────────────────────────────
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
# 翻译缓存读取（核心）
# ──────────────────────────────────────────────
def _get_title_zh(title: str) -> str | None:
    """从 translations.db 中获取翻译，若不存在或未翻译则返回 None"""
    row = _get_translation_db().execute(
        "SELECT translation FROM translations WHERE key = ?", (title,)
    ).fetchone()
    if row:
        zh = row["translation"]
        # 若翻译与原文相同，视为未翻译
        return zh if zh != title else None
    return None

def _get_name_zh(name: str) -> str | None:
    """从 translations.db 中获取演员名翻译"""
    return _get_title_zh(name)  # 演员名也使用相同缓存

# ──────────────────────────────────────────────
# 数据构建函数（注入 title_zh）
# ──────────────────────────────────────────────
def _build_summary(name: str) -> dict:
    src = _source(name)
    found = _find_star_dir(name)
    channel = found[0] if found else "default"

    base = {}
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
            base = {
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

    if not base and found:
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
        summary = baike.get("summary", "")
        if len(summary) > 120:
            summary = summary[:120] + "…"
        if not summary and info.get("biography"):
            bio = info["biography"]
            summary = (bio[:120] + "…") if len(bio) > 120 else bio
        base = {
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
            "summary": summary,
            "tmdb_url": info.get("tmdb_url", ""),
        }

    if not base:
        base = {
            "name": name, "channel": channel,
            "profile_image": None, "image_count": 0, "works_count": 0,
            "birthday": "", "birthplace": "", "summary": "", "tmdb_url": "",
        }
        # 在返回字典前加入作品日期查询
    first_work_date = None
    last_work_date = None
    db = _get_db()
    if db:
        row = db.execute("SELECT MIN(date) as first_date, MAX(date) as last_date FROM filmography WHERE name = ? AND date != ''", (name,)).fetchone()
        if row:
            first_work_date = row["first_date"]
            last_work_date = row["last_date"]
    base["first_work_date"] = first_work_date
    base["last_work_date"] = last_work_date

    # 注入中文名
    base["name_zh"] = _get_name_zh(name) or name
    return base

def _build_detail(name: str) -> dict:
    src = _source(name)
    found = _find_star_dir(name)
    channel = found[0] if found else "default"

    if src == "sqlite":
        db = _get_db()
        row = db.execute("SELECT * FROM actresses WHERE name = ?", (name,)).fetchone()
        if not row:
            abort(404)
        file_channel = row["channel"] or "default"
        baike = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM baike WHERE name = ?", (name,)).fetchall()}
        imgs = [{
            "filename": r["filename"],
            "url": f"/womenpic/images/{file_channel}/{name}/{r['filename']}",
            "size_kb": r["size_kb"],
        } for r in db.execute("SELECT filename, original_url, size_kb FROM images WHERE name = ? ORDER BY filename", (name,)).fetchall()]
        works = []
        for r in db.execute(
            "SELECT year, title, role, url, COALESCE(code,'') as code, "
            "COALESCE(date,'') as date, COALESCE(tags,'[]') as tags FROM filmography "
            "WHERE name = ? ORDER BY date DESC, id ASC", (name,)
        ):
            title_zh = _get_title_zh(r["title"])
            works.append({
                "year": r["year"], "title": r["title"], "title_zh": title_zh,
                "role": r["role"], "url": r["url"],
                "code": r["code"], "date": r["date"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
            })
        return {
            "name": row["name"],
            "name_zh": _get_name_zh(name) or row["name"],
            "channel": file_channel,
            "tmdb_id": row["tmdb_id"] or "",
            "tmdb_url": row["tmdb_url"] or "",
            "birthday": row["birthday"] or "",
            "birthplace": row["birthplace"] or "",
            "biography": row["biography"] or "",
            "baike": baike,
            "wiki_zh": row["wiki_zh"] or "",
            "wiki_en": row["wiki_en"] or "",
            "images": imgs,
            "filmography": works ,
            "filmography_total": len(works),
            "info": {},
        }

    # 文件夹模式（同样注入 title_zh）
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

    filmography = []
    for w in works :
        title = w.get("title", "")
        title_zh = _get_title_zh(title)
        filmography.append({
            "year": w.get("year"),
            "title": title,
            "title_zh": title_zh,
            "role": w.get("role"),
            "url": w.get("url", ""),
            "code": w.get("code", ""),
            "date": w.get("date", ""),
            "tags": w.get("tags", []),
        })

    return {
        "name": name,
        "name_zh": _get_name_zh(name) or name,
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
        "filmography": filmography,
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
# 翻译逻辑（批量翻译 + 缓存写入）
# ──────────────────────────────────────────────
def _translate_via_openrouter(titles: list[str]) -> list[str]:
    """调用翻译API，返回与输入顺序一致的译文列表"""
    if not API_KEY or not titles:
        return titles

    prompt = (
        "你是日文成人向标题翻译模型。\n"
        "将以下日文标题翻译为自然中文标题。\n"
        "要求：\n"
        "只输出中文标题\n"
        "不解释、不分析、不加引号\n"
        "保留人名、女优名、作品名\n"
        "理解成人向固定词汇和场景表达\n"
        "优化为中文资源站常用标题风格\n"
        "避免机械直译和日式语序\n"
        "保持原标题信息量，不扩写\n"
        "每行一个翻译，不要编号\n"
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
        "model": TRANSLATE_MODEL,
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
        resp = requests.post(OPENROUTER_API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        result = resp.json()

        content = None
        try:
            content = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            pass

        if content is None:
            _log("翻译API未返回content，使用原文")
            return titles

        # 按行分割译文，去除空行
        lines = [line.strip() for line in content.split("\n") if line.strip()]
        # 去除可能的行首序号
        cleaned = []
        for line in lines:
            if len(line) > 2 and line[0].isdigit() and (". " in line[:4] or ") " in line[:4]):
                line = line.split(". ", 1)[-1].split(") ", 1)[-1]
            cleaned.append(line)

        # 确保行数足够，不足则用原文补充
        if len(cleaned) < len(titles):
            cleaned += titles[len(cleaned):]
        return cleaned[:len(titles)]

    except Exception as e:
        _log(f"翻译API请求失败: {e}")
        return titles

def _batch_translate(texts: list[str]) -> list[str]:
    """批量翻译，优先使用缓存，返回与输入顺序一致的译文列表"""
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

    if uncached:
        _log(f"需要翻译 {len(uncached)} 个标题...")
        # 分批调用AI（每批最多50个，避免token溢出）
        batch_size = 50
        translated_all = []
        for i in range(0, len(uncached), batch_size):
            batch = uncached[i:i+batch_size]
            translated_batch = _translate_via_openrouter(batch)
            translated_all.extend(translated_batch)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for o, t in zip(uncached, translated_all):
            if t != o:
                db.execute(
                    "INSERT OR REPLACE INTO translations (key, translation, model, created_at) VALUES (?, ?, ?, ?)",
                    (o, t, TRANSLATE_MODEL, now)
                )
                cache_map[o] = t
            else:
                cache_map[o] = o
        db.commit()
    return [cache_map.get(t, t) for t in texts]

# ──────────────────────────────────────────────
# 作品搜索缓存重建（后台任务，更新 works_search.db）
# ──────────────────────────────────────────────
def _rebuild_works_cache():
    """将 translations.db 中已有的翻译同步到 works_search.db，并更新演员信息"""
    _log("开始重建作品搜索缓存...")
    cache_db = _get_works_cache_db()
    cache_db.execute("DELETE FROM works")
    cache_db.execute("DELETE FROM actress_info")
    cache_db.commit()

    all_stars = _scan_all_names()
    for star in all_stars:
        name = star["name"]
        channel = star["channel"]
        name_zh = _get_name_zh(name) or name
        cache_db.execute("INSERT OR IGNORE INTO actress_info (name, name_zh, channel) VALUES (?, ?, ?)",
                         (name, name_zh, channel))
        # 获取该演员的作品列表
        db = _get_db()
        if db:
            rows = db.execute(
                "SELECT title, COALESCE(code,'') as code, COALESCE(date,'') as date, url, COALESCE(tags,'[]') as tags "
                "FROM filmography WHERE name = ?", (name,)
            ).fetchall()
            for r in rows:
                title = r["title"]
                title_zh = _get_title_zh(title) or title
                try:
                    cache_db.execute(
                        "INSERT OR IGNORE INTO works (actress_name, title, title_zh, code, date, url, tags) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (name, title, title_zh, r["code"], r["date"], r["url"], r["tags"])
                    )
                except:
                    pass
    cache_db.commit()
    _log("作品搜索缓存重建完成")

# ──────────────────────────────────────────────
# 蓝图路由
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
    channel_labels = {"default": "全部女星", "jav": "Jav 日本女星"}
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

    matched_names = set()
    # 优先使用 works_search.db 搜索
    try:
        cache_db = _get_works_cache_db()
        # 搜索演员名
        rows = cache_db.execute(
            "SELECT DISTINCT name FROM actress_info WHERE name LIKE ? OR name_zh LIKE ?",
            (f"%{q}%", f"%{q}%")
        ).fetchall()
        for r in rows:
            matched_names.add(r["name"])
        # 搜索作品标题
        rows = cache_db.execute(
            "SELECT DISTINCT actress_name FROM works WHERE title LIKE ? OR title_zh LIKE ?",
            (f"%{q}%", f"%{q}%")
        ).fetchall()
        for r in rows:
            matched_names.add(r["actress_name"])
    except:
        # 回退到基本搜索
        matched_names = {x["name"] for x in all_items if q.lower() in x["name"].lower()}

    matched = [x for x in all_items if x["name"] in matched_names]
    items = [_build_summary(x["name"]) for x in matched]
    return jsonify({"count": len(items), "data": items})

@womenpic_bp.route("/api/translate", methods=["POST"])
def api_translate():
    """批量翻译接口（自动缓存）"""
    data = request.get_json()
    if not data or not isinstance(data.get("texts"), list):
        return jsonify({"error": "缺少 texts 参数"}), 400
    texts = data["texts"]
    translations = _batch_translate(texts)
    return jsonify({"translations": translations})

@womenpic_bp.route("/api/translate/all", methods=["POST"])
def api_translate_all():
    """翻译全部女星的全部作品标题（写入缓存），并同步重建搜索数据库"""
    db = _get_db()
    if not db:
        return jsonify({"error": "原始数据库不可用"}), 500
    titles = [row["title"] for row in db.execute("SELECT DISTINCT title FROM filmography").fetchall()]
    titles = list(set(titles))  # 去重
    _log(f"开始全局翻译，共 {len(titles)} 个标题...")
    _batch_translate(titles)  # 内部已处理缓存
    # 重建搜索数据库（后台线程，避免阻塞）
    app = current_app._get_current_object()
    def rebuild():
        with app.app_context():
            _rebuild_works_cache()
    threading.Thread(target=rebuild).start()
    return jsonify({"status": "completed", "count": len(titles)})

@womenpic_bp.route("/api/cache/rebuild", methods=["POST"])
def api_rebuild_cache():
    """手动触发重建 works_search.db"""
    app = current_app._get_current_object()
    def run():
        with app.app_context():
            _rebuild_works_cache()
    threading.Thread(target=run).start()
    return jsonify({"status": "started", "message": "作品搜索缓存重建已在后台启动。"})