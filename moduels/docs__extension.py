import os
import re
import json
import markdown
import requests
from datetime import datetime
from flask import Blueprint, jsonify, render_template_string


# ====================================================
# [工具类] WeatherService (全量接口恢复 + 内存缓存)
# ====================================================
class WeatherService:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://weather.com.cn/"
        }
        # 默认缓存状态，防止系统刚启动时没数据
        self.cached_text = "天气数据抓取中..."
        self.cached_raw = {
            'status': 'loading',
            'data': {'temp': '-', 'hum': '-', 'wd': '-', 'ws': '-', 'desc': '获取中', 'msg': '-'}
        }

    def refresh_weather(self, lat=31.093228, lon=109.914796, loc="巫山"):
        """后台专用的静默刷新函数，执行耗时网络请求"""
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
                
                # 1. 覆盖原始字典缓存
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
                # 2. 覆盖一句话文本缓存
                d = self.cached_raw['data']
                self.cached_text = f"{loc}: {d['desc']} {d['temp']}°C, {d['wd']}{d['ws']}, {d['msg']}"
        except Exception as e:
            print(f"[Docs] 天气刷新失败，保留历史缓存。原因: {e}")

    def get_weather(self):
        """恢复原功能：供 MD 提取字典字段 {{ get_weather_data().data.temp }}"""
        return self.cached_raw

    def get_weather_text(self):
        """恢复原功能：供 MD 直接输出一句话 {{ get_weather_text() }}"""
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
    """全量变量映射，彻底解决 undefined 报错"""
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


@docs_bp.route('/api/docs/list', methods=['GET'])
def list_docs():
    """扫描目录，按文件修改时间倒序返回"""
    if not os.path.exists(DOCS_DIR):
        os.makedirs(DOCS_DIR)
    
    files_with_time = []
    for f in os.listdir(DOCS_DIR):
        if f.endswith('.md'):
            path = os.path.join(DOCS_DIR, f)
            files_with_time.append({
                'name': f[:-3],
                'mtime': os.path.getmtime(path)
            })
    
    # 按修改时间(mtime)倒序排列
    files_with_time.sort(key=lambda x: x['mtime'], reverse=True)
    sorted_files = [item['name'] for item in files_with_time]
    
    return jsonify({"docs": sorted_files})

@docs_bp.route('/api/docs/content/<name>', methods=['GET'])
def doc_content_fast(name):
    """阶段一：读取缓存极速渲染 + 终极报错降级机制"""
    path = os.path.join(DOCS_DIR, f"{name}.md")
    if not os.path.exists(path):
        return jsonify({"error": "文档不存在"}), 404
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw_content = f.read()
            
        # ================= 核心修复：强制降级容错 =================
        try:
            # 尝试执行 Jinja2 动态变量注入
            rendered_md = render_template_string(raw_content, **get_dynamic_context())
        except Exception as render_err:
            # 只要 MD 文件里的变量写错了，或者后端挂了，直接拦截！
            print(f"[Docs] 变量注入失败，降级为静态文本展示。原因: {render_err}")
            # 原封不动地保留 MD 源码，确保排版和文字 100% 正常显示
            rendered_md = raw_content
        # ==========================================================

        html = markdown.markdown(rendered_md, extensions=['tables', 'fenced_code'])
        return jsonify({"title": name, "html": html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@docs_bp.route('/api/docs/refresh/<name>', methods=['GET'])
def doc_content_refresh(name):
    """阶段二：前端无感调用，后端强制抓取新天气并重新返回 HTML"""
    weather_service.refresh_weather()
    return doc_content_fast(name)

from flask import request
import re

# 安全路径过滤辅助函数
def get_safe_path(name):
    """过滤非法字符，防止目录遍历攻击 (如 ../)"""
    # 仅允许汉字、字母、数字、下划线和横线
    safe_name = re.sub(r'[^\w\u4e00-\u9fa5\-]', '', name)
    if not safe_name:
        raise ValueError("无效的文件名")
    return os.path.join(DOCS_DIR, f"{safe_name}.md")

@docs_bp.route('/api/docs/raw/<name>', methods=['GET'])
def get_raw_doc(name):
    """获取 Markdown 源码（用于在编辑框中回显）"""
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
    """新建或更新文档"""
    data = request.json
    name = data.get('name', '').strip()
    content = data.get('content', '')
    old_name = data.get('old_name', '').strip() # 用于重命名

    if not name:
        return jsonify({"error": "文档名称不能为空"}), 400

    try:
        # 处理重命名逻辑
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

import os
@docs_bp.route('/api/docs/search', methods=['GET'])
def search_docs():
    """全文本地搜索文档（排除目录前缀干扰，修复扩展名导致无法打开的问题）"""
    keyword = request.args.get('q', '').strip().lower()
    if not keyword:
        return jsonify({"results": []})

    results = []
    try:
        base_dir = os.path.dirname(get_safe_path('dummy_filename_for_dir_calc'))
        
        for filename in os.listdir(base_dir):
            if filename.startswith('.'):
                continue
                
            filepath = os.path.join(base_dir, filename)
            if not os.path.isfile(filepath):
                continue
            
            try:
                # 1. 修复打不开问题：去除物理扩展名，还原前端所需的标准 doc_id
                doc_id, _ = os.path.splitext(filename)
                
                # 2. 排除前缀干扰：剥离类似 folder_subfolder_ 的前缀，提取真实的纯文件名
                pure_name = doc_id.split('_')[-1]

                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                    # 搜索条件：仅让纯文件名和文件正文参与匹配，忽略模拟目录的标识前缀
                    if keyword in pure_name.lower() or keyword in content.lower():
                        title = pure_name
                        
                        # 尝试从正文首行提取标准 markdown 标题作为最优展示
                        first_line = content.split('\n')[0].strip() if content else ''
                        if first_line.startswith('#'):
                            title = first_line.lstrip('#').strip()
                            
                        # 标题防溢出截断
                        if len(title) > 60:
                            title = title[:60] + '...'
                            
                        results.append({
                            "id": doc_id,  # 必须返回包含层级但无扩展名的完整 doc_id（如 A_B_C）
                            "title": title
                        })
            except Exception:
                continue

        return jsonify({"results": results})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@docs_bp.route('/api/docs/rename', methods=['POST'])
def rename_doc():
    """重命名或移动文档（更改所属分类名）"""
    data = request.json
    old_name = data.get('oldName', '').strip()
    new_name = data.get('newName', '').strip()

    if not old_name or not new_name:
        return jsonify({"error": "原文件名和新文件名均不能为空"}), 400

    if old_name == new_name:
        return jsonify({"message": "文件名未变动"})

    try:
        old_path = get_safe_path(old_name)
        new_path = get_safe_path(new_name)

        if not os.path.exists(old_path):
            return jsonify({"error": "原文件不存在"}), 404

        if os.path.exists(new_path):
            return jsonify({"error": "目标文件名已存在，请先重命名或删除冲突文件"}), 409

        os.rename(old_path, new_path)
        return jsonify({"message": "重命名/移动成功"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@docs_bp.route('/api/docs/delete/<name>', methods=['DELETE'])
def delete_doc(name):
    """删除文档"""
    try:
        path = get_safe_path(name)
        if os.path.exists(path):
            os.remove(path)
            return jsonify({"message": "删除成功"})
        return jsonify({"error": "文档不存在"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500