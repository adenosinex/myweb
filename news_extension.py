import os, sqlite3, json, uuid, time, requests
from flask import Blueprint, request, jsonify
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from urllib.parse import urlparse

scrape_bp = Blueprint('scrape', __name__)

# --- 数据库配置：使用绝对路径确保稳定性 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "web_analysis.db")

executor = ThreadPoolExecutor(max_workers=5)
NODE_API_URL = "http://192.168.31.124:8901"
client = OpenAI(base_url='https://api.xiaomimimo.com/v1', api_key=os.getenv("MI_API_KEY"))

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    print(f"[*] 正在初始化数据库: {DB_PATH}")
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS webpages (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            clean_text TEXT,
            ai_summary TEXT,
            tags TEXT,
            status TEXT,
            error_msg TEXT,
            created_at DATETIME DEFAULT (datetime('now','localtime'))
        )
        """)
    print("[+] 数据库初始化成功。")

# 启动即执行初始化
init_db()

@scrape_bp.route("/api/scrape", methods=["POST"])
def scrape():
    data = request.get_json()
    url = data.get("url", "")
    domain = urlparse(url).netloc
    
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT url, title FROM webpages WHERE url LIKE ? AND title IS NOT NULL ORDER BY created_at DESC LIMIT 50", 
            (f"%{domain}%",)
        )
        history = [{"url": r[0], "title": r[1]} for r in cur.fetchall()]

    try:
        scan_resp = requests.post(f"{NODE_API_URL}/api/scan", json={"url": url}, timeout=20)
        scan_resp.raise_for_status()
        links = scan_resp.json().get("links", [])
        return jsonify({"status": "success", "links": links if links else history})
    except Exception as e:
        return jsonify({"status": "success", "links": history, "note": str(e)})

@scrape_bp.route("/api/analyze", methods=["POST"])
def analyze():
    url = request.get_json().get("url")
    record_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute("INSERT INTO webpages (id, url, status) VALUES (?, ?, ?)", (record_id, url, "scraping"))
    executor.submit(run_full_pipeline, record_id, url)
    return jsonify({"id": record_id, "status": "processing"})

def run_full_pipeline(record_id, url):
    try:
        node_resp = requests.post(f"{NODE_API_URL}/api/fetch", json={"url": url}, timeout=60)
        node_data = node_resp.json()
        title = node_data.get("title", "未命名标题")
        text = node_data.get("content", "")

        with get_conn() as conn:
            conn.execute("UPDATE webpages SET title=?, clean_text=?, status='analyzing' WHERE id=?", (title, text, record_id))

        prompt = f"分析文章并以JSON返回(summary:50字内, tags:数组)。标题：{title}\n正文：{text[:3000]}"
        ai_resp = client.chat.completions.create(
            model="mimo-v2-flash",
            messages=[{"role": "user", "content": prompt}]
        )
        raw_content = ai_resp.choices[0].message.content
        cleaned_json = raw_content.replace("```json", "").replace("```", "").strip()
        res = json.loads(cleaned_json)

        with get_conn() as conn:
            conn.execute("UPDATE webpages SET ai_summary=?, tags=?, status='completed' WHERE id=?", 
                        (res.get('summary', ''), json.dumps(res.get('tags', []), ensure_ascii=False), record_id))
    except Exception as e:
        with get_conn() as conn:
            conn.execute("UPDATE webpages SET status='failed', error_msg=? WHERE id=?", (str(e), record_id))

@scrape_bp.route("/api/results", methods=["GET"])
def results():
    try:
        with get_conn() as conn:
            rows = conn.execute("""
                SELECT id, url, title, ai_summary, tags, status, created_at, error_msg 
                FROM webpages ORDER BY created_at DESC LIMIT 50
            """).fetchall()
        return jsonify([{
            "id": r[0], "url": r[1], "title": r[2] or r[1],
            "summary": r[3], "tags": json.loads(r[4] or '[]'),
            "status": r[5], "created": r[6], "error": r[7]
        } for r in rows])
    except sqlite3.OperationalError:
        init_db() # 表不存在时修复
        return jsonify([])

@scrape_bp.route("/api/clear", methods=["POST"])
def clear_history():
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM webpages")
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500