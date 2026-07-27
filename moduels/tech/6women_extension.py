# -*- coding: utf-8 -*-
"""
WomenPic 服务端（单文件）
=======================
启动: python server.py
访问: http://localhost:5000          (前端页面)
      http://localhost:5000/womenpic/api/actresses  (API)
"""

import json
import sys
from pathlib import Path

from flask import Flask, Blueprint, jsonify, request, send_file, send_from_directory, abort
from flask_cors import CORS
 
# ── 工作目录：确保数据路径相对于此文件所在目录 ──
ROOT = Path(__file__).resolve().parent.parent.parent
 
WOMENS_DIR = ROOT / "db" / "womenpic"

print(WOMENS_DIR)
# ============================================================
# Blueprint: /womenpic
# ============================================================
womenpic_bp = Blueprint("womenpic", __name__, url_prefix="/womenpic")


def _safe_read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _scan_actresses() -> list[str]:
    if not WOMENS_DIR.exists():
        return []
    return sorted([d.name for d in WOMENS_DIR.iterdir() if d.is_dir()], key=str.lower)


def _build_summary(name: str) -> dict:
    folder = WOMENS_DIR / name
    info = _safe_read_json(folder / "info.json") or {}
    profile_img = info.get("downloaded_profile", "")
    images = info.get("images", [])
    profile_url = None
    if profile_img:
        profile_url = f"/womenpic/images/{name}/{profile_img}"
    elif images:
        profile_url = f"/womenpic/images/{name}/{images[0].get('file', '')}"

    img_count = len(list((folder / "images").glob("*"))) if (folder / "images").exists() else 0
    works_count = info.get("filmography", [])
    works_count = len(works_count) if isinstance(works_count, list) else 0

    baike = _safe_read_json(folder / "baike.json") or {}
    baike_summary = baike.get("summary", "")
    if len(baike_summary) > 120:
        baike_summary = baike_summary[:120] + "…"

    return {
        "name": name,
        "profile_image": profile_url,
        "image_count": img_count,
        "works_count": works_count,
        "birthday": info.get("birthday", baike.get("birthday", "")),
        "birthplace": info.get("birthplace", baike.get("birthplace", "")),
        "summary": baike_summary or (info.get("biography", "")[:120] + ("…" if len(info.get("biography", "")) > 120 else "")),
        "tmdb_url": info.get("tmdb_url", ""),
    }


# ── API 路由 ──

@womenpic_bp.route("/api/actresses")
def api_actresses():
    names = _scan_actresses()
    items = [_build_summary(name) for name in names]
    return jsonify({"count": len(items), "data": items})


@womenpic_bp.route("/api/actress/<path:name>")
def api_actress_detail(name):
    folder = WOMENS_DIR / name
    if not folder.exists():
        abort(404)

    info = _safe_read_json(folder / "info.json") or {}
    baike = _safe_read_json(folder / "baike.json") or {}

    img_dir = folder / "images"
    img_list = []
    if img_dir.exists():
        for f in sorted(img_dir.iterdir()):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                img_list.append({
                    "filename": f.name,
                    "url": f"/womenpic/images/{name}/{f.name}",
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })

    works = info.get("filmography", [])
    if not works and (folder / "filmography.json").exists():
        works = _safe_read_json(folder / "filmography.json") or []

    wiki_zh = (folder / "wiki_zh.txt").read_text(encoding="utf-8")[:2000] if (folder / "wiki_zh.txt").exists() else ""
    wiki_en = (folder / "wiki_en.txt").read_text(encoding="utf-8")[:2000] if (folder / "wiki_en.txt").exists() else ""

    return jsonify({
        "name": name,
        "tmdb_id": info.get("tmdb_id", ""),
        "tmdb_url": info.get("tmdb_url", ""),
        "birthday": info.get("birthday", baike.get("birthday", "")),
        "birthplace": info.get("birthplace", baike.get("birthplace", "")),
        "biography": info.get("biography", ""),
        "baike": baike,
        "wiki_zh": wiki_zh,
        "wiki_en": wiki_en,
        "images": img_list,
        "filmography": works[:100],
        "filmography_total": len(works),
        "info": {k: v for k, v in info.items() if k not in ("filmography", "images", "biography", "baidu_baike")},
    })


@womenpic_bp.route("/images/<path:name>/<path:filename>")
def serve_image(name, filename):
    img_path = WOMENS_DIR / name / "images" / filename
    if not img_path.exists():
        abort(404)
    return send_file(str(img_path.resolve()), mimetype="image/jpeg")


@womenpic_bp.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"count": 0, "data": []})
    all_names = _scan_actresses()
    matched = [n for n in all_names if q.lower() in n.lower()]
    items = [_build_summary(name) for name in matched]
    return jsonify({"count": len(items), "data": items})


# ============================================================
# Flask App
# ============================================================
def create_app():
    app = Flask(__name__, static_folder=None)
    CORS(app, resources={r"/womenpic/*": {"origins": "*"}})

    # 确保数据目录存在
    WOMENS_DIR.mkdir(parents=True, exist_ok=True)

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
    port = _os.environ.get("PORT", 5000)
    print("=" * 50)
    print("  🌟 WomenPic 服务启动")
    print(f"  前端页面: http://localhost:{port}")
    print(f"  API 接口: http://localhost:{port}/womenpic/api/actresses")
    print(f"  数据目录: {WOMENS_DIR}")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=True)
