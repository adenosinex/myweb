import os
import sqlite3
import json
import uuid
import time
import requests
import dashscope
from flask import Blueprint, request, jsonify
from concurrent.futures import ThreadPoolExecutor

scrape_bp = Blueprint('scrape', __name__)

DB_PATH = "web_analysis.db"
dashscope.api_key = os.getenv("MI_API_KEY")

executor = ThreadPoolExecutor(max_workers=5)
DEFAULT_NLP_MODEL = "mimo-v2-flash"

# 爬虫节点基础 URL（使用独立 Flask API 的地址）
NODE_API_URL = "http://192.168.31.124:8901"

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS webpages (
            id TEXT PRIMARY KEY,
            url TEXT,
            full_html TEXT,
            clean_text TEXT,
            ai_summary TEXT,
            tags TEXT,
            processing_time_sec REAL,
            status TEXT,
            error_msg TEXT,
            created_at DATETIME DEFAULT (datetime('now','localtime'))
        )
        """)
init_db()

def update_status(record_id, status, error_msg=""):
    with get_conn() as conn:
        conn.execute(
            "UPDATE webpages SET status=?, error_msg=? WHERE id=?",
            (status, error_msg, record_id)
        )

def parse_ai_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    if text.startswith("json"):
        text = text[4:].strip()
    try:
        return json.loads(text)
    except:
        return {"summary": text[:50], "tags": ["解析异常"]}

def process_url_task(record_id, url):
    start = time.time()
    try:
        update_status(record_id, "scraping")
        # 调用节点 /apinews/fetch 抓取详情
        node_resp = requests.post(
            f"{NODE_API_URL}/apinews/fetch",
            json={"url": url},
            timeout=60
        )
        node_resp.raise_for_status()
        node_data = node_resp.json()

        if "error" in node_data:
            raise Exception(node_data["error"])

        clean_text = node_data.get("content", "")
        title = node_data.get("title", "")

        if len(clean_text) < 50:
            raise Exception("提取的正文过短，可能被拦截或非文章页")

        update_status(record_id, "analyzing")
        prompt = f"""
        请作为专业新闻编辑分析以下新闻内容。
        要求：
        1. 总结核心内容，严格控制在 50 字以内。
        2. 提取 3 到 5 个核心关键词。
        3. 严格按照以下 JSON 格式返回，不要包含任何多余文字：

        {{
        "summary":"这里是50字以内的摘要",
        "tags":["关键词1","关键词2","关键词3"]
        }}

        新闻标题：{title}
        新闻正文：
        {clean_text[:4000]}
        """
        ai = dashscope.Generation.call(
            model=DEFAULT_NLP_MODEL,
            prompt=prompt,
            result_format="message"
        )

        if ai.status_code != 200:
            raise Exception(ai.message)

        raw = ai.output.choices[0].message.content
        data = parse_ai_json(raw)

        duration = round(time.time() - start, 2)

        with get_conn() as conn:
            conn.execute("""
            UPDATE webpages
            SET clean_text=?, ai_summary=?, tags=?, processing_time_sec=?, status='completed'
            WHERE id=?
            """, (
                clean_text,
                data.get("summary", ""),
                json.dumps(data.get("tags", []), ensure_ascii=False),
                duration,
                record_id
            ))

    except Exception as e:
        update_status(record_id, "failed", str(e))

@scrape_bp.route("/news/scrape", methods=["POST"])
def scrape():
    print("=== /scrape called ===")
    data = request.get_json()
    if not data or "url" not in data:
        return jsonify({"error": "缺少 url 参数"}), 400

    url = data["url"]
    print(f"URL: {url}")

    try:
        # 调用节点 /apinews/scan 获取首页文章链接
        scan_resp = requests.post(
            f"{NODE_API_URL}/apinews/scan",
            json={"url": url},
            timeout=40
        )
        print(f"Node response status: {scan_resp.status_code}")
        scan_resp.raise_for_status()
        scan_data = scan_resp.json()
        print(f"Node data: {scan_data}")

        links = scan_data.get("links", [])
        print(f"Links count: {len(links)}")

        if not links:
            return jsonify({
                "status": "empty",
                "count": 0,
                "message": "未在首页识别到符合规则的文章链接"
            }), 200

        # 为每个链接创建数据库记录并提交异步任务
        for link in links:
            record_id = str(uuid.uuid4())
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO webpages (id, url, status) VALUES (?, ?, ?)",
                    (record_id, link, "pending")
                )
            executor.submit(process_url_task, record_id, link)

        return jsonify({
            "status": "started",
            "count": len(links)
        })

    except requests.exceptions.RequestException as e:
        print(f"节点请求失败: {e}")
        return jsonify({"error": f"节点请求失败: {str(e)}"}), 500
    except Exception as e:
        print(f"处理异常: {e}")
        return jsonify({"error": str(e)}), 500
@scrape_bp.route("/apinews/news/results", methods=["GET"])
def results():
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
        SELECT id, url, ai_summary, tags, status, processing_time_sec, created_at, error_msg
        FROM webpages
        ORDER BY created_at DESC
        LIMIT 50
        """)
        rows = c.fetchall()

    data = []
    for r in rows:
        data.append({
            "id": r[0],
            "url": r[1],
            "summary": r[2],
            "tags": json.loads(r[3]) if r[3] else [],
            "status": r[4],
            "time": r[5],
            "created": r[6],
            "error": r[7]
        })
    return jsonify(data)