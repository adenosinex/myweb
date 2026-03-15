import os, sqlite3, json, requests, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, request, jsonify
 
from openai import OpenAI
from bs4 import BeautifulSoup
from urllib.parse import urlparse

scrape_bp = Blueprint('smart_scraper', __name__, url_prefix='/smart_scraper')
 

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "web_cache.db")
NODE_API_URL = "http://192.168.31.124:8901"
client = OpenAI(base_url='https://api.xiaomimimo.com/v1', api_key=os.getenv("MI_API_KEY"))

def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS webpages (
            url TEXT PRIMARY KEY, title TEXT, clean_text TEXT, 
            pub_date TEXT, ai_summary TEXT, tags TEXT, raw_html TEXT
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS site_configs (
            domain TEXT PRIMARY KEY, url_pattern TEXT, selector TEXT, regex TEXT
        )""")
init_db()

# ================= 1. 获取资源接口 (依赖 Node 节点返回 raw_html, title, content) =================
def fetch_single_html(url, bypass_cache):
    if not bypass_cache:
        with get_conn() as conn:
            row = conn.execute("SELECT raw_html, title, clean_text FROM webpages WHERE url=?", (url,)).fetchone()
            if row and row[0]: 
                return url, row[0], row[1], row[2], True

    try:
        resp = requests.post(f"{NODE_API_URL}/api/fetch", json={"url": url}, timeout=30)
        data = resp.json()
        raw_html = data.get("raw_html", "")
        title = data.get("title", "")
        content = data.get("content", "")
        
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO webpages (url, raw_html, title, clean_text) VALUES (?, ?, ?, ?) 
                ON CONFLICT(url) DO UPDATE SET raw_html=excluded.raw_html, title=excluded.title, clean_text=excluded.clean_text
            """, (url, raw_html, title, content))
        return url, raw_html, title, content, False
    except Exception as e:
        return url, f"Error: {str(e)}", "", "", False

@scrape_bp.route("/html", methods=["POST"])
def api_html():
    urls = request.json.get("urls", [])
    bypass_cache = request.json.get("bypass_cache", False)
    
    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_single_html, url, bypass_cache): url for url in urls}
        for future in as_completed(futures):
            url, html, title, content, cached = future.result()
            results[url] = {"html": html, "title": title, "content": content, "cached": cached}
            
    return jsonify({"status": "success", "data": results})

# ================= 2. 本地解析接口 (仅负责时间提取) =================
@scrape_bp.route("/parse", methods=["POST"])
def api_parse():
    data = request.json
    url = data.get("url", "")
    html = data.get("html", "")
    selector = data.get("selector", "")
    regex = data.get("regex", "")
    
    if not html or html.startswith("Error:"):
        return jsonify({"status": "error", "error": "无效的 HTML 内容"})

    # 从数据库拿回 Node 节点已经解析好的标题和正文
    with get_conn() as conn:
        row = conn.execute("SELECT title, clean_text FROM webpages WHERE url=?", (url,)).fetchone()
    title = row[0] if row else "未知标题"
    content = row[1] if row else ""

    # 使用 BeautifulSoup + Regex 提取发布时间
    soup = BeautifulSoup(html, 'html.parser')
    target_text = ""
    if selector:
        try:
            for s in selector.split(','):
                el = soup.select_one(s.strip())
                if el:
                    target_text = el.get_text(separator=' ', strip=True)
                    break
        except: pass
        
    if not target_text: 
        target_text = soup.get_text(separator=' ', strip=True)[:1000]
    
    pub_date = ""
    if regex and target_text:
        try:
            match = re.search(regex, target_text)
            if match: pub_date = match.group(1).strip()
        except: pass
        
    if not pub_date:
        sample = re.sub(r'\s+', ' ', target_text[:100])
        pub_date = f"未命中: {sample}..."

    with get_conn() as conn:
        conn.execute("UPDATE webpages SET pub_date=? WHERE url=?", (pub_date, url))

    return jsonify({"status": "success", "title": title, "content": content, "pub_date": pub_date})

# ================= 3. AI 调用接口 =================
@scrape_bp.route("/ai", methods=["POST"])
def api_ai():
    content = request.json.get("content", "")
    custom_prompt = request.json.get("prompt", "")
    
    if not content:
        return jsonify({"status": "error", "error": "正文为空"})

    prompt = custom_prompt if custom_prompt else f"分析文章，严格返回 JSON格式: {{\"summary\":\"50字摘要\",\"tags\":[\"标签1\",\"标签2\"]}}\n正文:{content[:2000]}"
    
    try:
        res = client.chat.completions.create(model="mimo-v2-flash", messages=[{"role": "user", "content": prompt}], temperature=0.1)
        cleaned = res.choices[0].message.content.replace("```json","").replace("```","").strip()
        analysis = json.loads(cleaned)
        return jsonify({"status": "success", **analysis})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "summary": "AI分析失败", "tags": []})

# ================= 配置与扫描路由 =================
@scrape_bp.route("/config/get", methods=["GET"])
def get_config():
    with get_conn() as conn:
        row = conn.execute("SELECT selector, regex FROM site_configs WHERE domain=?", (request.args.get("domain"),)).fetchone()
    return jsonify({"selector": row[0] if row else "", "regex": row[1] if row else ""})

@scrape_bp.route("/config/save", methods=["POST"])
def save_config():
    data = request.json
    with get_conn() as conn:
        conn.execute("INSERT INTO site_configs (domain, selector, regex) VALUES (?, ?, ?) ON CONFLICT(domain) DO UPDATE SET selector=excluded.selector, regex=excluded.regex", 
                     (data['domain'], data.get('selector'), data.get('regex')))
    return jsonify({"status": "success"})

@scrape_bp.route("/scan_index", methods=["POST"])
def scan():
    try:
        resp = requests.post(f"{NODE_API_URL}/api/scan", json={"url": request.json.get("url")}, timeout=40)
        return jsonify({"status": "success", "links": resp.json().get("links", [])})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})