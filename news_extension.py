import os, sqlite3, json, requests, re
from flask import Blueprint, request, jsonify
 
from openai import OpenAI
from bs4 import BeautifulSoup
from urllib.parse import urlparse

# 定义 Blueprint 及其唯一路由前缀
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
            domain TEXT PRIMARY KEY, selector TEXT, regex TEXT
        )""")
        cols = [c[1] for c in conn.execute("PRAGMA table_info(webpages)").fetchall()]
        if 'raw_html' not in cols:
            conn.execute("ALTER TABLE webpages ADD COLUMN raw_html TEXT")
init_db()

DEFAULT_CONFIGS = {
    "www.people.com.cn": {
        "selector": "p.sou, .box01 .fl, .text_dot_line",
        "regex": r"(\d{4}年\d{1,2}月\d{1,2}日)"
    },
    "global": { "selector": "", "regex": r"(\d{4}[年-]\d{1,2}[月-]\d{1,2}.*?\d{1,2}:\d{1,2})" }
}

def get_site_config(domain):
    with get_conn() as conn:
        row = conn.execute("SELECT selector, regex FROM site_configs WHERE domain=?", (domain,)).fetchone()
    return {"selector": row[0], "regex": row[1]} if row else DEFAULT_CONFIGS.get(domain, DEFAULT_CONFIGS["global"])

def apply_custom_extract(html, url):
    domain = urlparse(url).netloc
    config = get_site_config(domain)
    selector, regex = config['selector'], config['regex']
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
    if not target_text: target_text = soup.get_text(separator=' ', strip=True)[:4000]
    if regex and target_text:
        try:
            match = re.search(regex, target_text)
            return match.group(1).strip() if match else ""
        except: pass
    return target_text[:50]

@scrape_bp.route("/config/get", methods=["GET"])
def get_config_api():
    domain = request.args.get("domain")
    return jsonify(get_site_config(domain))

@scrape_bp.route("/config/save", methods=["POST"])
def save_config_api():
    data = request.json
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO site_configs (domain, selector, regex) VALUES (?, ?, ?)
            ON CONFLICT(domain) DO UPDATE SET selector=excluded.selector, regex=excluded.regex
        """, (data['domain'], data.get('selector'), data.get('regex')))
    return jsonify({"status": "success"})

@scrape_bp.route("/fetch_content", methods=["POST"])
def fetch():
    url = request.json.get("url")
    with get_conn() as conn:
        row = conn.execute("SELECT title, clean_text, pub_date, raw_html FROM webpages WHERE url=?", (url,)).fetchone()
    
    if row and row[3]:
        new_date = apply_custom_extract(row[3], url)
        if new_date != row[2]:
            with get_conn() as conn: conn.execute("UPDATE webpages SET pub_date=? WHERE url=?", (new_date, url))
        return jsonify({"status": "success", "title": row[0], "content": row[1], "pub_date": new_date, "cached": True})

    try:
        resp = requests.post(f"{NODE_API_URL}/api/fetch", json={"url": url}, timeout=60)
        data = resp.json()
        raw_html = data.get("raw_html", "")
        pub_date = apply_custom_extract(raw_html, url)
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO webpages (url, title, clean_text, pub_date, raw_html) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET title=excluded.title, clean_text=excluded.clean_text, 
                pub_date=excluded.pub_date, raw_html=excluded.raw_html
            """, (url, data.get('title',''), data.get('content',''), pub_date, raw_html))
        return jsonify({"status": "success", "title": data.get('title'), "content": data.get('content'), "pub_date": pub_date})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@scrape_bp.route("/analyze_ai", methods=["POST"])
def analyze():
    url = request.json.get("url")
    with get_conn() as conn:
        row = conn.execute("SELECT title, clean_text, ai_summary, tags FROM webpages WHERE url=?", (url,)).fetchone()
    if row and row[2]:
        return jsonify({"status": "success", "summary": row[2], "tags": json.loads(row[3] or '[]'), "cached": True})
    
    prompt = f"分析文章，JSON返回: {{\"summary\":\"50字摘要\",\"tags\":[\"标签\"]}}\n标题:{row[0]}\n正文:{row[1][:2500]}"
    res = client.chat.completions.create(model="mimo-v2-flash", messages=[{"role": "user", "content": prompt}], temperature=0.1)
    analysis = json.loads(res.choices[0].message.content.replace("```json","").replace("```",""))
    with get_conn() as conn:
        conn.execute("UPDATE webpages SET ai_summary=?, tags=? WHERE url=?", (analysis['summary'], json.dumps(analysis['tags']), url))
    return jsonify({"status": "success", **analysis})

@scrape_bp.route("/scan_index", methods=["POST"])
def scan():
    url = request.json.get("url")
    try:
        resp = requests.post(f"{NODE_API_URL}/api/scan", json={"url": url}, timeout=40)
        return jsonify({"status": "success", "links": resp.json().get("links", [])})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@scrape_bp.route("/db_reset", methods=["POST"])
def clear_db():
    with get_conn() as conn: conn.execute("DELETE FROM webpages")
    return jsonify({"status": "success"})