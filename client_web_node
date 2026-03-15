import time
import re
import requests
from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright
from readability import Document
from bs4 import BeautifulSoup
import html2text

flask_app = Flask(__name__)

# ================= 辅助工具：正则提取时间 =================
def extract_pub_date(html_content):
    """从 HTML 中精准匹配发布时间"""
    soup = BeautifulSoup(html_content, 'html.parser')
    text = soup.get_text(separator=' ', strip=True)[:3000] # 只扫前3000字，提高效率
    
    patterns = [
        r"《.*?》\s*（\s*(\d{4}年\d{1,2}月\d{1,2}日).*?版\s*）", # 报纸格式
        r"(\d{4}年\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{1,2})",       # 标准中文格式
        r"(\d{4}-\d{1,2}-\d{1,2}\s*\d{1,2}:\d{1,2})",          # ISO格式
        r"(\d{4}年\d{1,2}月\d{1,2}日)",                        # 仅日期
        r"(\d{4}-\d{1,2}-\d{1,2})"                             # 仅日期横杠
    ]
    
    for p in patterns:
        match = re.search(p, text)
        if match:
            return match.group(1).strip()
    return ""

# ================= 1. 扫描首页 (Playwright) =================
@flask_app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"error": "缺少 url 参数"}), 400
        
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            # 屏蔽图片加载，加速扫描
            page.route("**/*.{png,jpg,jpeg,gif,webp,svg}", lambda route: route.abort())
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            
            # 提取链接和标题
            links = page.evaluate('''() => {
                return Array.from(document.querySelectorAll('a'))
                    .map(a => ({title: a.innerText.trim(), url: a.href}))
                    .filter(item => item.title.length > 5 && item.url.startsWith('http'));
            }''')
            browser.close()
            
            # 去重
            seen = set()
            unique_links = []
            for l in links:
                if l['url'] not in seen:
                    seen.add(l['url'])
                    unique_links.append(l)
            
            return jsonify({"links": unique_links[:40]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ================= 2. 抓取正文 (Requests + Playwright) =================
@flask_app.route("/api/fetch", methods=["POST"])
def api_fetch():
    data = request.get_json()
    url = data.get("url")
    start_time = time.time()
    
    # 策略 A: Requests 极速抓取
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.encoding = resp.apparent_encoding
        html = resp.text
        
        doc = Document(html)
        h = html2text.HTML2Text()
        h.ignore_links = True
        content = h.handle(doc.summary()).strip()
        
        if len(content) > 100: # 如果内容足够长，认为成功
            return jsonify({
                "title": doc.title(),
                "content": content,
                "raw_html": html # 将原始HTML传回Core解析
                # "pub_date": extract_pub_date(html),
                # "method": "requests",
                # "fetch_time": round(time.time() - start_time, 2)
            })
    except:
        pass

    # 策略 B: Playwright 兜底
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            html = page.content()
            doc = Document(html)
            h = html2text.HTML2Text()
            h.ignore_links = True
            content = h.handle(doc.summary()).strip()
            browser.close()
            
            return jsonify({
                "title": doc.title(),
                "content": content,
                "pub_date": extract_pub_date(html),
                "method": "playwright",
                "fetch_time": round(time.time() - start_time, 2)
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=8901, threaded=True)