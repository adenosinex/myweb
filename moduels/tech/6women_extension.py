# -*- coding: utf-8 -*-
"""
WomenPic 服务端（多渠道路由）
=============================
启动: python server.py
打包: python build_db.py（生成 db/womenpic.db + db/images.zip）

渠道子目录:
  db/womenpic/default/  — 默认渠道
  db/womenpic/jav/      — jav 渠道（javbus主+javdb备）

读取优先级: SQLite > 文件夹
图片读取:   images.zip > images/ 文件夹
"""

import io
import json
import sqlite3
import zipfile
from pathlib import Path

from flask import Flask, Blueprint, jsonify, request, send_file, send_from_directory, abort
from flask_cors import CORS

# ── 路径 ──
ROOT = Path(__file__).resolve().parent.parent.parent
WOMENS_DIR = ROOT / "db" / "womenpic"
DB_PATH = ROOT / "db" / "womenpic.db"
ZIP_PATH = ROOT / "db" / "images.zip"

# ── 渠道子目录 ──
CHANNELS = {
    "default": WOMENS_DIR / "default",
    "jav":     WOMENS_DIR / "jav",
}
# 兼容旧版（根目录直接放数据）
LEGACY_DIR = WOMENS_DIR  # 旧版无子目录

# ── 全局缓存 ──
_db_conn: sqlite3.Connection | None = None
_zip_file: zipfile.ZipFile | None = None
_use_sqlite = False


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


def _init_mode():
    global _use_sqlite
    _use_sqlite = DB_PATH.exists()
    if _use_sqlite:
        _get_db()
        _get_zip()
        print(f"  📊 数据模式: SQLite + ZIP ({DB_PATH.stat().st_size/1024:.0f}KB / {ZIP_PATH.stat().st_size/1024/1024:.1f}MB)")
    else:
        print(f"  📁 数据模式: 文件夹")
    all_names = _scan_all_names()
    print(f"  👩 女星数量: {len(all_names)}")


# ============================================================
# 数据读取层
# ============================================================

def _safe_read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _get_channel_dirs() -> list[tuple[str, Path]]:
    """返回所有有效的渠道目录 [(channel_name, path), ...]。"""
    dirs = []
    # 新版：子目录
    for ch, path in CHANNELS.items():
        if path.exists() and path.is_dir():
            dirs.append((ch, path))
    # 兼容旧版：根目录直接有数据
    if LEGACY_DIR.exists() and not dirs:
        # 检查是否有旧数据格式的子目录
        has_data = any(
            d.is_dir() and not d.name.startswith(".") and d.name not in CHANNELS
            for d in LEGACY_DIR.iterdir()
        )
        if has_data:
            dirs.append(("default", LEGACY_DIR))
    return dirs


def _scan_all_names() -> list[dict]:
    """
    获取所有女星，返回 [{name, channel}, ...]。
    SQLite + 文件夹去重，SQLite 优先。
    """
    names_map = {}  # name → channel

    db = _get_db()
    if db:
        rows = db.execute(
            "SELECT name, COALESCE(channel, 'default') as channel FROM actresses ORDER BY name COLLATE NOCASE"
        ).fetchall()
        for r in rows:
            names_map[r["name"]] = r["channel"]

    # 文件夹补充
    for ch, ch_dir in _get_channel_dirs():
        for d in ch_dir.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                if d.name not in names_map:
                    names_map[d.name] = ch

    return [{"name": n, "channel": c} for n, c in sorted(names_map.items(), key=lambda x: x[0].lower())]


def _find_star_dir(name: str) -> tuple[str, Path] | None:
    """根据女星名查找所在渠道和数据目录，返回 (channel, dir_path)。"""
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

    # 文件夹查找
    for ch, ch_dir in _get_channel_dirs():
        star_dir = ch_dir / name
        if star_dir.exists():
            return (ch, star_dir)
    return None


def _source(name: str) -> str:
    """返回数据来源: 'sqlite' 或 'folder'。"""
    db = _get_db()
    if db:
        row = db.execute("SELECT 1 FROM actresses WHERE name = ?", (name,)).fetchone()
        if row:
            return "sqlite"
    found = _find_star_dir(name)
    return "folder" if found else "none"


def _build_summary(name: str) -> dict:
    """构建首页摘要卡片数据。"""
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

    # folder fallback
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

    # channel from info.json
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
    """构建详情页数据。"""
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

    # folder fallback
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

    # works: 优先 works (v2), 回退 filmography (v1)
    works = info.get("works", [])
    if not works:
        works = info.get("filmography", [])
    if not works and (folder / "filmography.json").exists():
        works = _safe_read_json(folder / "filmography.json") or []

    wiki_zh = (folder / "wiki_zh.txt").read_text(encoding="utf-8")[:2000] if (folder / "wiki_zh.txt").exists() else ""
    wiki_en = (folder / "wiki_en.txt").read_text(encoding="utf-8")[:2000] if (folder / "wiki_en.txt").exists() else ""

    # 构建 baike 兼容数据（从 core/body 提取）
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

    # 合并 baike.json 和 info.json 的 core/body 数据
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
    """图片服务：优先 ZIP，回退文件夹。"""
    zf = _get_zip()
    if zf:
        zpath = f"{channel}/{name}/{filename}"
        try:
            data = zf.read(zpath)
            return send_file(io.BytesIO(data), mimetype="image/jpeg")
        except KeyError:
            pass
        # 兼容旧 ZIP 格式（无 channel 前缀）
        try:
            data = zf.read(f"{name}/{filename}")
            return send_file(io.BytesIO(data), mimetype="image/jpeg")
        except KeyError:
            pass

    # 文件夹：先在指定 channel 下找
    ch_dir = CHANNELS.get(channel, LEGACY_DIR)
    img_path = ch_dir / name / "images" / filename
    if img_path.exists():
        return send_file(str(img_path.resolve()), mimetype="image/jpeg")

    # 兼容旧版：无 channel 子目录
    img_path = LEGACY_DIR / name / "images" / filename
    if img_path.exists():
        return send_file(str(img_path.resolve()), mimetype="image/jpeg")

    abort(404)


# ============================================================
# Blueprint
# ============================================================
womenpic_bp = Blueprint("womenpic", __name__, url_prefix="/womenpic")


@womenpic_bp.route("/api/actresses")
def api_actresses():
    channel_filter = request.args.get("channel", "").strip()
    all_items = _scan_all_names()

    if channel_filter:
        all_items = [x for x in all_items if x["channel"] == channel_filter]

    items = [_build_summary(x["name"]) for x in all_items]

    # 汇总渠道统计
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
    """返回可用渠道列表及计数。"""
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


# ============================================================
# Flask App
# ============================================================
def create_app():
    app = Flask(__name__, static_folder=None)
    CORS(app, resources={r"/womenpic/*": {"origins": "*"}})

    # 初始化数据模式
    _init_mode()

    # 确保渠道目录存在
    for ch_dir in CHANNELS.values():
        ch_dir.mkdir(parents=True, exist_ok=True)

    app.register_blueprint(womenpic_bp)

    @app.route("/")
    def index():
        return send_from_directory(str(ROOT), "index.html")

    @app.route("/<path:filename>")
    def serve_frontend(filename):
        target = ROOT / filename
        if target.exists() and target.is_file():
            return send_from_directory(str(ROOT), filename)
        if not filename.startswith("womenpic/"):
            return send_from_directory(str(ROOT), "index.html")
        return "Not Found", 404

    return app


app = create_app()

if __name__ == "__main__":
    import os as _os
    port = int(_os.environ.get("PORT", 5000))

    print("=" * 50)
    print("  🌟 WomenPic 服务启动")
    print(f"  前端: http://localhost:{port}")
    print(f"  API:  http://localhost:{port}/womenpic/api/actresses")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=True)
