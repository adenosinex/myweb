"""个人知识笔记系统：Flask Blueprint（兼容旧版 /docapi 接口）。

接入示例：
    from knowledge_notes_backend import document_bp
    app.register_blueprint(document_bp)

依赖：flask、PyYAML、requests。数据目录默认仍为 db/docsnew_data，
可用 NOTES_DOCS_DIR 环境变量覆盖。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path

import requests
import yaml
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
document_bp = Blueprint("document", __name__)

DOCS_DIR = os.environ.get("NOTES_DOCS_DIR", "db/docsnew_data")
INBOX_DIR = os.path.join(DOCS_DIR, "inbox")
ARCHIVE_DIR = os.path.join(DOCS_DIR, "archive")
TRASH_DIR = DOCS_DIR + "_trash"  # 保持回收站在扫描目录之外
MAX_FILES_PER_FOLDER = 100
FILE_INDEX: dict[str, dict] = {}
INDEX_LOCK = threading.RLock()

for directory in (DOCS_DIR, INBOX_DIR, ARCHIVE_DIR, TRASH_DIR):
    os.makedirs(directory, exist_ok=True)


def now_timestamp() -> int:
    return int(time.time())


def timestamp_to_string(ts) -> str:
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def sanitize_doc_id(value) -> str:
    """兼容旧版：删除非法文件名字符，保留中文、空格及连字符。"""
    if value is None:
        return ""
    name = str(value).strip()
    # 不允许将 /../ 等路径当作文件名，避免客户端影响磁盘目录。
    if "/" in name or "\\" in name:
        return ""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")


def valid_doc_id(value) -> bool:
    return isinstance(value, str) and bool(value) and value == sanitize_doc_id(value) and len(value) <= 180


def doc_path(relative_path: str) -> Path:
    root = Path(DOCS_DIR).resolve()
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root) or target.suffix.lower() != ".md":
        raise ValueError("非法文档路径")
    return target


def get_target_folder(base_dir: str) -> str:
    """每个编号文件夹最多保存 100 篇文档，兼容已有 001、002 结构。"""
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    existing = sorted(int(p.name) for p in base.iterdir() if p.is_dir() and p.name.isdigit())
    number = existing[-1] if existing else 1
    target = base / f"{number:03d}"
    target.mkdir(parents=True, exist_ok=True)
    if sum(p.is_file() and p.suffix.lower() == ".md" for p in target.iterdir()) >= MAX_FILES_PER_FOLDER:
        target = base / f"{number + 1:03d}"
        target.mkdir(parents=True, exist_ok=True)
    return str(target)


def parse_frontmatter(content: str) -> tuple[dict, str]:
    content = content if isinstance(content, str) else ""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", content, re.S)
    if not match:
        return {}, content
    try:
        data = yaml.safe_load(match.group(1)) or {}
        return (data if isinstance(data, dict) else {}), content[match.end():]
    except yaml.YAMLError:
        # 不吞掉原始内容中的数据；保存前端传来的原文时不破坏旧格式。
        return {}, content


def build_frontmatter(meta: dict, content: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"---\n{front}---\n{content}"


def parse_meta_timestamp(meta: dict, *keys) -> int | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, datetime):
            return int(value.timestamp())
        if isinstance(value, date):
            return int(datetime.combine(value, datetime.min.time()).timestamp())
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
        if isinstance(value, str) and (value := value.strip()):
            if value.isdigit():
                return int(value)
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    return None


def get_created_time(meta: dict, full_path: Path) -> int:
    return parse_meta_timestamp(meta, "created_time", "created_at", "created") or int(full_path.stat().st_ctime)


def get_update_time(meta: dict, full_path: Path) -> int:
    try:
        return int(full_path.stat().st_mtime)
    except OSError:
        return parse_meta_timestamp(meta, "updated_time", "updated_at", "updated") or now_timestamp()


def write_atomic(path: Path, content: str) -> None:
    """同目录落盘并原子替换，避免保存中断留下半篇 Markdown。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".note_", suffix=".tmp", delete=False) as tmp:
            temp_name = tmp.name
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def build_doc_index(full_path: Path, rel_path: str, raw_content: str, meta: dict | None = None) -> dict:
    if meta is None:
        meta, pure_content = parse_frontmatter(raw_content)
    else:
        _, pure_content = parse_frontmatter(raw_content)
    tags = meta.get("tags", [])
    if not isinstance(tags, list):
        tags = [str(tags)] if tags else []
    tags = [str(t) for t in tags if t is not None]
    status = "archive" if "archive" in Path(rel_path).parts else "active"
    created = get_created_time(meta, full_path)
    updated = get_update_time(meta, full_path)
    preview = re.sub(r"\s+", " ", re.sub(r"[`*_#>\[\]]", "", pure_content)).strip()[:150]
    return {
        "id": str(meta.get("id") or full_path.stem),
        "path": rel_path.replace("\\", "/"),
        "title": str(meta.get("title") or full_path.stem),
        "status": status,
        "is_pinned": 1 if meta.get("pinned") else 0,
        "tags": tags,
        "tags_cache": ",".join(tags),
        "created_time": created,
        "update_time": updated,
        "created_at": timestamp_to_string(created),
        "updated_at": timestamp_to_string(updated),
        "file_size": full_path.stat().st_size,
        "word_count": len(pure_content.strip()),
        "preview": preview,
    }


def ensure_created_time_in_frontmatter(path: Path, raw: str, meta: dict) -> tuple[str, dict]:
    if parse_meta_timestamp(meta, "created_time", "created_at", "created"):
        return raw, meta
    stat = path.stat()
    new_meta = dict(meta, created_time=int(stat.st_ctime))
    _, body = parse_frontmatter(raw)
    updated = build_frontmatter(new_meta, body)
    try:
        write_atomic(path, updated)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return updated, new_meta
    except OSError:
        logger.exception("迁移创建时间失败: %s", path)
        return raw, meta


def build_memory_index() -> None:
    with INDEX_LOCK:
        new_index: dict[str, dict] = {}
        root = Path(DOCS_DIR)
        for directory, _, files in os.walk(root):
            for filename in files:
                if not filename.lower().endswith(".md"):
                    continue
                path = Path(directory) / filename
                try:
                    if path.is_symlink():
                        continue
                    raw = path.read_text(encoding="utf-8")
                    meta, _ = parse_frontmatter(raw)
                    raw, meta = ensure_created_time_in_frontmatter(path, raw, meta)
                    rel = path.relative_to(root).as_posix()
                    item = build_doc_index(path, rel, raw, meta)
                    if item["id"] in new_index:
                        logger.warning("重复文档 ID，已跳过 %s", path)
                        continue
                    new_index[item["id"]] = item
                except (OSError, ValueError, yaml.YAMLError):
                    logger.exception("索引文档失败: %s", path)
        FILE_INDEX.clear()
        FILE_INDEX.update(new_index)


def current_doc(doc_id: str) -> tuple[dict, Path] | None:
    item = FILE_INDEX.get(doc_id)
    if not item:
        return None
    return item, doc_path(item["path"])


def json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def error(message: str, status: int):
    return jsonify({"error": message}), status


def update_meta_timestamp(meta: dict, created: int, status: str) -> None:
    now = now_timestamp()
    meta.update(id=meta.get("id"), created_time=created, updated_time=now,
                updated=timestamp_to_string(now), status=status)


def generate_tags_from_ai(content: str) -> list[str]:
    api_key = os.getenv("OP_API_KEY")
    if not api_key:
        return ["API_KEY_MISSING"]
    snippet = content[:800].strip()
    if not snippet:
        return []
    payload = {
        "model": "deepseek/deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "You are a keyword extraction assistant. Extract 3 to 5 highly relevant keywords in Chinese. Return ONLY a comma-separated list of keywords, NO JSON, NO explanations."},
            {"role": "user", "content": f"Text:\n\n{snippet}"},
        ],
        "max_tokens": 100,
        "temperature": 0.2,
    }
    try:
        response = requests.post("https://openrouter.ai/api/v1/chat/completions", json=payload,
                                 headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                                 timeout=20)
        response.raise_for_status()
        value = response.json()["choices"][0]["message"]["content"].strip()
        value = re.sub(r"<think>.*?</think>", "", value, flags=re.S).strip()
        try:
            parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I))
            candidates = parsed.get("tags", []) if isinstance(parsed, dict) else parsed
            if not isinstance(candidates, list):
                raise ValueError("响应不是标签列表")
        except (ValueError, TypeError):
            candidates = re.split(r"[,，、\n;；]+", value.strip(' "`[]{}'))
        tags: list[str] = []
        for item in candidates:
            tag = re.sub(r"[^\w\u4e00-\u9fff-]", "", str(item)).strip()
            if 1 < len(tag) <= 15 and tag.lower() not in ("tags", "keywords", "关键词", "标签") and tag not in tags:
                tags.append(tag)
        return tags[:5]
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        logger.exception("AI 标签请求失败")
        return ["AI_ERROR"]


# 保留旧版蓝图启动时的索引扫描行为
build_memory_index()


@document_bp.route("/docapi/init", methods=["POST"])
def init_system():
    build_memory_index()
    return jsonify({"message": "扫描重建索引完成", "total": len(FILE_INDEX)})


@document_bp.route("/docapi/documents", methods=["GET"])
@document_bp.route("/docapi/skip/documents", methods=["GET"])
def list_documents():
    with INDEX_LOCK:
        docs = sorted((dict(d) for d in FILE_INDEX.values()),
                      key=lambda d: (d["is_pinned"], d["update_time"]), reverse=True)
    return jsonify({"docs": docs})


@document_bp.route("/docapi/document/<doc_id>", methods=["GET"])
@document_bp.route("/docapi/skip/document/<doc_id>", methods=["GET"])
def get_document(doc_id):
    with INDEX_LOCK:
        found = current_doc(doc_id)
        if not found:
            return error("文档不在索引中", 404)
        old, path = found
        if not path.is_file():
            return error("物理文件丢失", 404)
        try:
            raw = path.read_text(encoding="utf-8")
            meta, _ = parse_frontmatter(raw)
            item = build_doc_index(path, old["path"], raw, meta)
        except OSError:
            logger.exception("读取文档失败")
            return error("文档读取失败", 500)
        FILE_INDEX[doc_id] = item
        return jsonify({"id": doc_id, "content": raw, "meta": item})


@document_bp.route("/docapi/document/save", methods=["POST"])
@document_bp.route("/docapi/skip/document/save", methods=["POST"])
def save_document():
    data = json_body()
    doc_id, raw = data.get("id"), data.get("content", "")
    if not valid_doc_id(doc_id):
        return error("缺少文档ID或文件名包含非法字符", 400)
    if not isinstance(raw, str):
        return error("文档内容必须为字符串", 400)
    with INDEX_LOCK:
        existing = FILE_INDEX.get(doc_id)
        status = existing["status"] if existing else "active"
        if existing:
            path = doc_path(existing["path"])
            if not path.is_file():
                return error("物理文件丢失，请先同步索引", 404)
            relative = existing["path"]
        else:
            path = Path(get_target_folder(ARCHIVE_DIR if status == "archive" else INBOX_DIR)) / f"{doc_id}.md"
            relative = path.relative_to(Path(DOCS_DIR)).as_posix()
            if path.exists():
                return error("同名文件已存在，请先同步索引", 409)
        meta, body = parse_frontmatter(raw)
        created = (existing or {}).get("created_time") or parse_meta_timestamp(meta, "created_time", "created_at")
        if not created:
            try:
                created = int(data.get("created_time") or now_timestamp())
            except (TypeError, ValueError, OverflowError):
                created = now_timestamp()
        meta["id"] = doc_id
        update_meta_timestamp(meta, int(created), status)
        final_content = build_frontmatter(meta, body)
        try:
            write_atomic(path, final_content)
            item = build_doc_index(path, relative, final_content, meta)
        except OSError:
            logger.exception("保存失败: %s", doc_id)
            return error("文件写入失败", 500)
        FILE_INDEX[doc_id] = item
        return jsonify({"message": "保存成功", "meta": item})


@document_bp.route("/docapi/document/rename", methods=["POST"])
def rename_document():
    data = json_body()
    old_id, requested = data.get("old_id"), data.get("new_id")
    if not old_id or not requested:
        return error("缺少 old_id 或 new_id", 400)
    new_id = sanitize_doc_id(requested)
    if not valid_doc_id(new_id):
        return error("非法的文件名", 400)
    with INDEX_LOCK:
        found = current_doc(old_id)
        if not found:
            return error("文档不存在", 404)
        info, old_path = found
        if new_id == old_id:
            return jsonify({"message": "ID 未变化", "old_id": old_id, "new_id": new_id, "meta": info})
        if new_id in FILE_INDEX:
            return error(f"ID「{new_id}」已存在", 409)
        if not old_path.is_file():
            return error("物理文件丢失", 404)
        new_path = old_path.with_name(f"{new_id}.md")
        if new_path.exists():
            return error(f"目标文件 {new_id}.md 已存在", 409)
        try:
            raw = old_path.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(raw)
            meta["id"] = new_id
            update_meta_timestamp(meta, get_created_time(meta, old_path), info["status"])
            content = build_frontmatter(meta, body)
            write_atomic(new_path, content)
            try:
                old_path.unlink()
            except OSError:
                new_path.unlink(missing_ok=True)
                raise
            rel = new_path.relative_to(Path(DOCS_DIR).resolve()).as_posix()
            item = build_doc_index(new_path, rel, content, meta)
        except OSError:
            logger.exception("重命名失败: %s -> %s", old_id, new_id)
            return error("重命名失败", 500)
        FILE_INDEX.pop(old_id, None)
        FILE_INDEX[new_id] = item
        return jsonify({"message": "重命名成功", "old_id": old_id, "new_id": new_id, "meta": item})


@document_bp.route("/docapi/document/delete", methods=["POST"])
def delete_document():
    doc_id = json_body().get("id")
    if not doc_id:
        return error("缺少文档ID", 400)
    with INDEX_LOCK:
        found = current_doc(doc_id)
        if not found:
            return error("文档不存在", 404)
        _, path = found
        if not path.is_file():
            FILE_INDEX.pop(doc_id, None)
            return jsonify({"message": "文件已不存在，索引已清理", "id": doc_id})
        trash_name = f"{time.time_ns()}_{path.name}"
        destination = Path(TRASH_DIR) / trash_name
        try:
            shutil.move(str(path), str(destination))
        except OSError:
            logger.exception("删除失败: %s", doc_id)
            return error("删除失败", 500)
        FILE_INDEX.pop(doc_id, None)
        return jsonify({"message": "已移入回收站", "id": doc_id, "trash": trash_name})


@document_bp.route("/docapi/document/action", methods=["POST"])
def document_action():
    data = json_body()
    doc_id, action = data.get("id"), data.get("action")
    if action not in {"toggle_pin", "toggle_archive"}:
        return error("不支持的操作", 400)
    with INDEX_LOCK:
        found = current_doc(doc_id)
        if not found:
            return error("文档不存在", 404)
        info, original = found
        if not original.is_file():
            return error("物理文件丢失", 404)
        try:
            raw = original.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(raw)
            status = info["status"]
            target = original
            if action == "toggle_pin":
                meta["pinned"] = not bool(meta.get("pinned", False))
            else:
                status = "active" if status == "archive" else "archive"
                target_folder = get_target_folder(ARCHIVE_DIR if status == "archive" else INBOX_DIR)
                target = Path(target_folder) / original.name
                if target.exists():
                    return error("目标分类下存在同名文件", 409)
            meta["id"] = doc_id
            update_meta_timestamp(meta, get_created_time(meta, original), status)
            content = build_frontmatter(meta, body)
            # 先创建目标完整内容，成功后才移除旧文件，避免移动后写入失败。
            write_atomic(target, content)
            if target != original:
                try:
                    original.unlink()
                except OSError:
                    target.unlink(missing_ok=True)
                    raise
            rel = target.resolve().relative_to(Path(DOCS_DIR).resolve()).as_posix()
            item = build_doc_index(target, rel, content, meta)
        except OSError:
            logger.exception("文档操作失败: %s", doc_id)
            return error("文档操作失败", 500)
        FILE_INDEX[doc_id] = item
        return jsonify({"message": "操作成功", "meta": item})


@document_bp.route("/docapi/document/ai/tags", methods=["POST"])
def get_ai_tags():
    _, content = parse_frontmatter(str(json_body().get("content", "")))
    return jsonify({"tags": generate_tags_from_ai(content)})


@document_bp.route("/docapi/ai/batch_tags", methods=["POST"])
def batch_ai_tags():
    with INDEX_LOCK:
        pending = [dict(x) for x in FILE_INDEX.values() if not x["tags"] or x["tags"] in (["未分类"], ["笔记"])]
    if not pending:
        return jsonify({"message": "无待处理文档", "processed": 0})
    processed = 0
    for item in pending[:2]:  # 与旧版一致，每次最多 2 篇
        with INDEX_LOCK:
            found = current_doc(item["id"])
            if not found or not found[1].is_file():
                continue
            info, path = found
            try:
                raw = path.read_text(encoding="utf-8")
                meta, body = parse_frontmatter(raw)
            except OSError:
                continue
        tags = generate_tags_from_ai(body)
        if not tags or tags[0] in {"AI_ERROR", "API_KEY_MISSING"}:
            continue
        with INDEX_LOCK:
            found = current_doc(item["id"])
            if not found or not found[1].is_file():
                continue
            info, path = found
            try:
                current_raw = path.read_text(encoding="utf-8")
                current_meta, current_body = parse_frontmatter(current_raw)
                current_meta["tags"] = tags
                current_meta["id"] = item["id"]
                update_meta_timestamp(current_meta, get_created_time(current_meta, path), info["status"])
                result = build_frontmatter(current_meta, current_body)
                write_atomic(path, result)
                FILE_INDEX[item["id"]] = build_doc_index(path, info["path"], result, current_meta)
                processed += 1
            except OSError:
                logger.exception("AI 标签保存失败: %s", item["id"])
    return jsonify({"message": "批量处理完成", "processed": processed})


@document_bp.route("/docapi/document/<doc_id>/related", methods=["GET"])
def get_related(doc_id):
    with INDEX_LOCK:
        current = FILE_INDEX.get(doc_id)
        if not current:
            return jsonify([])
        target = set(current["tags"]) - {"未分类", "笔记"}
        if not target:
            return jsonify([])
        matched = [{"id": other_id, "title": item["title"],
                    "overlap": len(target & set(item["tags"]))}
                   for other_id, item in FILE_INDEX.items() if other_id != doc_id]
    return jsonify(sorted((item for item in matched if item["overlap"]),
                          key=lambda item: item["overlap"], reverse=True)[:5])


@document_bp.route("/docapi/timeline", methods=["GET"])
def get_timeline():
    mode = request.args.get("mode", "created")
    mode = mode if mode in {"created", "updated"} else "created"
    month = request.args.get("month")
    field = "created_time" if mode == "created" else "update_time"
    counts: dict[str, int] = {}
    dates: list[str] = []
    with INDEX_LOCK:
        for item in FILE_INDEX.values():
            ts = item.get(field)
            if not ts:
                continue
            day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            dates.append(day)
            if month and day[:7] != month:
                continue
            counts[day] = counts.get(day, 0) + 1
    return jsonify({"mode": mode, "month": month, "counts": counts,
                    "min_date": min(dates, default=None), "max_date": max(dates, default=None),
                    "total": sum(counts.values())})


@document_bp.route("/docapi/search", methods=["GET"])
def search_documents():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"results": []})
    needle = query.lower()
    only_tags = needle.startswith("tag:")
    if only_tags:
        needle = needle[4:].strip()
    found: list[dict] = []
    with INDEX_LOCK:
        for item in FILE_INDEX.values():
            tags = item["tags_cache"].lower()
            if only_tags:
                if needle in tags:
                    found.append(dict(item))
                continue
            if needle in item["title"].lower() or needle in tags:
                found.append(dict(item))
                continue
            try:
                if needle in doc_path(item["path"]).read_text(encoding="utf-8").lower():
                    found.append(dict(item))
            except OSError:
                logger.debug("搜索时文档不可读: %s", item["id"])
    return jsonify({"results": found})


_L1_CODES = {"pol", "his", "mil", "eco", "soc", "psy", "know", "exe", "med", "out",
             "tec", "fan", "cul", "dai", "adu", "unk"}
_CAT_P1 = re.compile(r"^\[([a-z]{2,8})\s+([a-z]{2,8})\]\s*(.*)$", re.I)
_CAT_P2 = re.compile(r"^([a-z]{2,8})\s+([a-z]{2,8})\s+(.+)$", re.I)


def _parse_category(title):
    title = str(title or "").strip()
    match = _CAT_P1.match(title)
    if match:
        return match.group(1).lower(), match.group(2).lower(), match.group(3).strip() or "未命名笔记"
    match = _CAT_P2.match(title)
    if match and match.group(1).lower() in _L1_CODES:
        return match.group(1).lower(), match.group(2).lower(), match.group(3).strip()
    return "unk", "", title or "未命名笔记"


@document_bp.route("/docapi/stats", methods=["GET"])
def get_stats():
    now = now_timestamp()
    categories_map: dict[str, dict] = {}
    articles: list[dict] = []
    words = pinned = archived = today = week = month = 0
    with INDEX_LOCK:
        docs = [dict(item) for item in FILE_INDEX.values()]
    for item in docs:
        group, subgroup, title = _parse_category(item.get("title"))
        wc = int(item.get("word_count") or 0)
        ct = int(item.get("created_time") or 0)
        words += wc
        pinned += item.get("is_pinned") == 1
        archived += item.get("status") == "archive"
        today += ct >= now - 86400
        week += ct >= now - 7 * 86400
        month += ct >= now - 30 * 86400
        section = categories_map.setdefault(group, {"code": group, "count": 0, "words": 0,
                                                    "max": 0, "min": 0, "children": {}})
        section["count"] += 1
        section["words"] += wc
        section["max"] = max(section["max"], wc)
        section["min"] = min(section["min"], wc) if section["min"] else wc
        if subgroup:
            child = section["children"].setdefault(subgroup, {"code": subgroup, "count": 0, "words": 0})
            child["count"] += 1
            child["words"] += wc
        articles.append({"id": item["id"], "title": title, "words": wc,
                         "created_time": ct, "update_time": int(item.get("update_time") or 0)})
    sections = []
    for section in categories_map.values():
        section["avg"] = round(section["words"] / section["count"], 1)
        section["children"] = [{**child, "avg": round(child["words"] / child["count"], 1)}
                               for child in sorted(section["children"].values(), key=lambda c: -c["count"])]
        sections.append(section)
    sections.sort(key=lambda s: -s["count"])
    count = len(docs)
    return jsonify({
        "total": {"docs": count, "words": words, "avg_words": round(words / count, 1) if count else 0,
                  "pinned": pinned, "archived": archived, "active": count - archived},
        "time": {"today": today, "week": week, "month": month},
        "categories": sections,
        "top_longest": sorted(articles, key=lambda a: -a["words"])[:5],
        "top_shortest": sorted((a for a in articles if a["words"] > 0), key=lambda a: a["words"])[:5],
        "generated_at": now,
    })
