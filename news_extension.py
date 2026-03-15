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
        # 1. 网页数据缓存表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS webpages (
            url TEXT PRIMARY KEY, title TEXT, clean_text TEXT, 
            pub_date TEXT, ai_summary TEXT, tags TEXT, raw_html TEXT
        )""")
        # 2. 站点规则配置表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS site_configs (
            domain TEXT PRIMARY KEY, 
            index_regex TEXT,   -- 首页链接过滤正则
            selector TEXT,      -- 时间选择器
            regex TEXT          -- 时间清洗正则
        )""")
        # 数据库平滑升级检查
        cols = [c[1] for c in conn.execute("PRAGMA table_info(site_configs)").fetchall()]
        if 'index_regex' not in cols:
            conn.execute("ALTER TABLE site_configs ADD COLUMN index_regex TEXT")
init_db()

# ================= 1. 资源获取接口 (批量/并发) =================
def fetch_single_html(url, bypass_cache):
    if not bypass_cache:
        with get_conn() as conn:
            row = conn.execute("SELECT raw_html, title, clean_text FROM webpages WHERE url=?", (url,)).fetchone()
            if row and row[0]: 
                return url, row[0], row[1], row[2], True

    try:
        resp = requests.post(f"{NODE_API_URL}/api/fetch", json={"url": url}, timeout=30)
        resp.raise_for_status()
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

# ================= 2. 结构化解析接口 (时间提取) =================
@scrape_bp.route("/parse", methods=["POST"])
def api_parse():
    data = request.json
    url = data.get("url", "")
    html = data.get("html", "")
    selector = data.get("selector", "")
    regex = data.get("regex", "")
    
    if not html or html.startswith("Error:"):
        return jsonify({"status": "error", "error": "无效的内容"})

    with get_conn() as conn:
        row = conn.execute("SELECT title, clean_text FROM webpages WHERE url=?", (url,)).fetchone()
    
    title = row[0] if row else "未捕获标题"
    content = row[1] if row else ""

    soup = BeautifulSoup(html, 'html.parser')
    target_text = ""
    if selector:
        try:
            el = soup.select_one(selector)
            if el: target_text = el.get_text(separator=' ', strip=True)
        except: pass
    
    if not target_text: 
        target_text = soup.get_text(separator=' ', strip=True)[:1000]
    
    pub_date = ""
    if regex and target_text:
        match = re.search(regex, target_text)
        if match: pub_date = match.group(1).strip()
        
    if not pub_date:
        sample = re.sub(r'\s+', ' ', target_text[:60])
        pub_date = f"未命中: {sample}..."

    with get_conn() as conn:
        conn.execute("UPDATE webpages SET pub_date=? WHERE url=?", (pub_date, url))

    return jsonify({"status": "success", "title": title, "content": content, "pub_date": pub_date})

# ================= 3. AI 调用接口 =================
@scrape_bp.route("/ai", methods=["POST"])
def api_ai():
    content = request.json.get("content", "")
    custom_prompt = request.json.get("prompt", "")
    
    if not content or len(content) < 10:
        return jsonify({"status": "error", "error": "正文内容太短，无法分析"})

    prompt = custom_prompt if custom_prompt else f"分析文章，严格返回 JSON格式: {{\"summary\":\"50字摘要\",\"tags\":[\"标签\"]}}\n正文:{content[:2000]}"
    
    try:
        res = client.chat.completions.create(model="mimo-v2-flash", messages=[{"role": "user", "content": prompt}], temperature=0.1)
        cleaned = res.choices[0].message.content.replace("```json","").replace("```","").strip()
        analysis = json.loads(cleaned)
        return jsonify({"status": "success", **analysis})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "summary": "AI分析失败", "tags": []})

# ================= 4. 配置与扫描路由 =================
@scrape_bp.route("/config/get", methods=["GET"])
def get_config():
    domain = request.args.get("domain")
    with get_conn() as conn:
        row = conn.execute("SELECT index_regex, selector, regex FROM site_configs WHERE domain=?", (domain,)).fetchone()
    if row:
        return jsonify({"index_regex": row[0], "selector": row[1], "regex": row[2]})
    return jsonify({"index_regex": "", "selector": "", "regex": ""})

@scrape_bp.route("/config/save", methods=["POST"])
def save_config():
    data = request.json
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO site_configs (domain, index_regex, selector, regex) VALUES (?, ?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET 
                index_regex=excluded.index_regex, 
                selector=excluded.selector, 
                regex=excluded.regex
        """, (data['domain'], data.get('index_regex'), data.get('selector'), data.get('regex')))
    return jsonify({"status": "success"})

@scrape_bp.route("/scan_index", methods=["POST"])
def scan():
    url = request.json.get("url")
    domain = urlparse(url).netloc
    
    with get_conn() as conn:
        row = conn.execute("SELECT index_regex FROM site_configs WHERE domain=?", (domain,)).fetchone()
    
    link_regex = request.json.get("link_regex") or (row[0] if row else None)
    
    try:
        resp = requests.post(f"{NODE_API_URL}/api/scan", json={"url": url}, timeout=40)
        raw_links = resp.json().get("links", [])
        
        if link_regex:
            try:
                raw_links = [l for l in raw_links if re.search(link_regex, l.get('url',''))]
            except: pass # 正则错误则不匹配
        
        return jsonify({"status": "success", "links": raw_links})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})

@scrape_bp.route("/db_reset", methods=["POST"])
def clear_db():
    with get_conn() as conn: conn.execute("DELETE FROM webpages")
    return jsonify({"status": "success"})