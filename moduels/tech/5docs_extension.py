import os
import re
import json
import markdown
import requests
import csv
import html
from io import StringIO, BytesIO
from datetime import datetime 
from flask import Blueprint, jsonify, render_template_string, request, send_file

# ====================================================
# [工具类] WeatherService
# ====================================================
class WeatherService: 
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://weather.com.cn/"
        }
        self.cached_text = "天气数据抓取中..."
        self.cached_raw = {
            'status': 'loading',
            'data': {'temp': '-', 'hum': '-', 'wd': '-', 'ws': '-', 'desc': '获取中', 'msg': '-'}
        }

    def refresh_weather(self, lat=31.093228, lon=109.914796, loc="巫山"):
        try:
            ts = int(datetime.now().timestamp() * 1000)
            url_less = f"https://mpf.weather.com.cn/mpf_v3/webgis/minute?lat={lat}&lon={lon}&callback=fc5m&_={ts}"
            url_sk = f"https://forecast.weather.com.cn/town/api/v1/sk?lat={lat}&lng={lon}&callback=getDataSK&_={ts}"
            
            c_less = requests.get(url_less, headers=self.headers, timeout=3).text
            c_sk = requests.get(url_sk, headers=self.headers, timeout=3).text
            
            m_less = re.search(r'fc5m\((.*?)\)', c_less, re.DOTALL)
            m_sk = re.search(r'getDataSK\((.*?)\)', c_sk, re.DOTALL)
            
            if m_sk and m_less:
                sk = json.loads(m_sk.group(1))
                less = json.loads(m_less.group(1))
                
                self.cached_raw = {
                    'status': 'success',
                    'data': {
                        'temp': sk.get('temp', 'N/A'),
                        'hum': sk.get('humidity', 'N/A'),
                        'wd': sk.get('WD', 'N/A'),
                        'ws': sk.get('WS', 'N/A'),
                        'desc': sk.get('weather', '未知'),
                        'msg': less.get('msg', '暂无预报')
                    }
                }
                d = self.cached_raw['data']
                self.cached_text = f"{loc}: {d['desc']} {d['temp']}°C, {d['wd']}{d['ws']}, {d['msg']}"
        except Exception as e:
            print(f"[Docs] 天气刷新失败: {e}")

    def get_weather(self):
        return self.cached_raw

    def get_weather_text(self):
        return self.cached_text


# ====================================================
# [业务逻辑] Docs Blueprint
# ====================================================
docs_bp = Blueprint('docs', __name__)
DOCS_DIR = 'db/docs_data'
weather_service = WeatherService()


def get_system_stats():
    return {"cpu": "12%", "mem": "45%", "disk": "normal"}

def get_dynamic_context():
    return {
        'weather': weather_service.get_weather_text,
        'get_weather_text': weather_service.get_weather_text,
        'weather_raw': weather_service.get_weather,
        'get_weather_data': weather_service.get_weather,
        'sys': get_system_stats,
        'sys_stats': get_system_stats,
        'current_time': lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'now': lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

def get_safe_path(name):
    safe_name = re.sub(r'[^\w\u4e00-\u9fa5\-]', '', name)
    if not safe_name:
        raise ValueError("无效的文件名")
    return os.path.join(DOCS_DIR, f"{safe_name}.md")

# ================= Frontmatter 辅助函数 =================
def parse_md_frontmatter(content):
    """解析 Markdown 文件，返回 tags 和 正文内容"""
    tags = ""
    if content.startswith('---\n'):
        parts = content.split('---\n', 2)
        if len(parts) >= 3:
            for line in parts[1].strip().split('\n'):
                if line.startswith('tags:'):
                    tags = line.split(':', 1)[1].strip().strip('"').strip("'")
            return tags, parts[2]
    return tags, content

def update_md_tags_in_file(filepath, new_tags):
    """更新或插入文件的 tags"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if content.startswith('---\n'):
        parts = content.split('---\n', 2)
        if len(parts) >= 3:
            meta_lines = parts[1].split('\n')
            new_meta = []
            tag_found = False
            for line in meta_lines:
                if line.startswith('tags:'):
                    new_meta.append(f'tags: {new_tags}')
                    tag_found = True
                else:
                    new_meta.append(line)
            if not tag_found:
                new_meta.append(f'tags: {new_tags}')
            
            new_content = f"---\n{chr(10).join(new_meta)}\n---{parts[2]}"
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            return
            
    # 如果没有 Frontmatter，直接添加
    new_content = f"---\ntags: {new_tags}\n---\n{content}"
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)


@docs_bp.route('/api/docs/list', methods=['GET'])
def list_docs():
    """扫描目录，返回包含元数据的对象列表"""
    if not os.path.exists(DOCS_DIR):
        os.makedirs(DOCS_DIR)
    
    docs_data = []
    for f in os.listdir(DOCS_DIR):
        if f.endswith('.md'):
            path = os.path.join(DOCS_DIR, f)
            doc_id = f[:-3]
            mtime = os.path.getmtime(path)
            
            try:
                with open(path, 'r', encoding='utf-8') as file:
                    content = file.read()
                
                tags, pure_content = parse_md_frontmatter(content)
                title = doc_id.split('_')[-1]
                
                # 尝试从正文首行提取标题
                m = re.search(r'^(?:#+\s*)?([^\n]+)', pure_content.strip(), re.MULTILINE)
                if m and len(m.group(1).strip()) < 40:
                    title = m.group(1).strip()
                    
                docs_data.append({
                    'id': doc_id,
                    'title': title,
                    'tags': tags,
                    'mtime': mtime
                })
            except Exception:
                pass
    
    docs_data.sort(key=lambda x: x['mtime'], reverse=True)
    return jsonify({"docs": docs_data})


@docs_bp.route('/api/docs/content/<name>', methods=['GET'])
def doc_content_fast(name):
    path = os.path.join(DOCS_DIR, f"{name}.md")
    if not os.path.exists(path):
        return jsonify({"error": "文档不存在"}), 404
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw_content = f.read()
            
        _, pure_content = parse_md_frontmatter(raw_content)

        try:
            rendered_md = render_template_string(pure_content, **get_dynamic_context())
        except Exception as render_err:
            rendered_md = pure_content

        html_content = markdown.markdown(rendered_md, extensions=['tables', 'fenced_code'])
        return jsonify({"title": name, "html": html_content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@docs_bp.route('/api/docs/refresh/<name>', methods=['GET'])
def doc_content_refresh(name):
    weather_service.refresh_weather()
    return doc_content_fast(name)


@docs_bp.route('/api/docs/raw/<name>', methods=['GET'])
def get_raw_doc(name):
    try:
        path = get_safe_path(name)
        if not os.path.exists(path):
            return jsonify({"error": "文档不存在"}), 404
        with open(path, 'r', encoding='utf-8') as f:
            return jsonify({"title": name, "content": f.read()})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@docs_bp.route('/api/docs/save', methods=['POST'])
def save_doc():
    data = request.json
    name = data.get('name', '').strip()
    content = data.get('content', '')
    old_name = data.get('old_name', '').strip()

    if not name:
        return jsonify({"error": "文档名称不能为空"}), 400

    try:
        if old_name and old_name != name:
            old_path = get_safe_path(old_name)
            if os.path.exists(old_path):
                os.remove(old_path)

        path = get_safe_path(name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return jsonify({"message": "保存成功"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@docs_bp.route('/api/docs/export_untagged', methods=['GET'])
def export_untagged():
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['id', 'title', 'tags', 'content_snippet'])
    
    for f in os.listdir(DOCS_DIR):
        if not f.endswith('.md'): continue
        path = os.path.join(DOCS_DIR, f)
        doc_id = f[:-3]
        
        with open(path, 'r', encoding='utf-8') as file:
            content = file.read()
            
        tags, pure_content = parse_md_frontmatter(content)
        if not tags.strip():
            title = doc_id.split('_')[-1]
            m = re.search(r'^(?:#+\s*)?([^\n]+)', pure_content.strip(), re.MULTILINE)
            if m and len(m.group(1).strip()) < 40:
                title = m.group(1).strip()
            
            # 转HTML去标签提取纯文本
            html_text = markdown.markdown(pure_content)
            clean_text = re.sub(r'<[^>]+>', '', html_text)
            clean_text = html.unescape(clean_text)
            snippet = clean_text.replace('\n', ' ').replace('\r', '')
            snippet = re.sub(r'\s+', ' ', snippet).strip()[:100]
            
            cw.writerow([doc_id, title, '', snippet])
            
    output = BytesIO(si.getvalue().encode('utf-8-sig'))
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name='untagged_md.csv')


@docs_bp.route('/api/docs/import_tags', methods=['POST'])
def import_tags():
    if 'file' not in request.files: return jsonify({"error": "没有文件部分"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "未选择文件"}), 400

    try:
        stream = StringIO(file.stream.read().decode('utf-8-sig'))
        csv_reader = csv.DictReader(stream)
        updated_count = 0
        for row in csv_reader:
            doc_id = row.get('id', '').strip()
            new_tags = row.get('tags', '').strip()
            
            if doc_id and new_tags:
                path = os.path.join(DOCS_DIR, f"{doc_id}.md")
                if os.path.exists(path):
                    update_md_tags_in_file(path, new_tags)
                    updated_count += 1
                    
        return jsonify({"status": "success", "updated": updated_count})
    except Exception as e:
        return jsonify({"error": f"CSV解析错误: {str(e)}"}), 500


@docs_bp.route('/api/docs/search', methods=['GET'])
def search_docs():
    keyword = request.args.get('q', '').strip().lower()
    if not keyword: return jsonify({"results": []})
    results = []
    try:
        base_dir = os.path.dirname(get_safe_path('dummy_filename_for_dir_calc'))
        for filename in os.listdir(base_dir):
            if filename.startswith('.') or not filename.endswith('.md'): continue
            filepath = os.path.join(base_dir, filename)
            doc_id = filename[:-3]
            pure_name = doc_id.split('_')[-1]

            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
                
            tags, pure_content = parse_md_frontmatter(content)
            
            if keyword in pure_name.lower() or keyword in pure_content.lower() or keyword in tags.lower():
                title = pure_name
                first_line = pure_content.strip().split('\n')[0].strip() if pure_content else ''
                if first_line.startswith('#'):
                    title = first_line.lstrip('#').strip()
                if len(title) > 60: title = title[:60] + '...'
                    
                results.append({"id": doc_id, "title": title})

        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@docs_bp.route('/api/docs/rename', methods=['POST'])
def rename_doc():
    data = request.json
    old_name = data.get('oldName', '').strip()
    new_name = data.get('newName', '').strip()
    if not old_name or not new_name: return jsonify({"error": "原文件名和新文件名均不能为空"}), 400
    if old_name == new_name: return jsonify({"message": "文件名未变动"})

    try:
        old_path = get_safe_path(old_name)
        new_path = get_safe_path(new_name)
        if not os.path.exists(old_path): return jsonify({"error": "原文件不存在"}), 404
        if os.path.exists(new_path): return jsonify({"error": "目标文件名已存在"}), 409
        os.rename(old_path, new_path)
        return jsonify({"message": "重命名/移动成功"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@docs_bp.route('/api/docs/delete/<name>', methods=['DELETE'])
def delete_doc(name):
    try:
        path = get_safe_path(name)
        if os.path.exists(path):
            os.remove(path)
            return jsonify({"message": "删除成功"})
        return jsonify({"error": "文档不存在"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500