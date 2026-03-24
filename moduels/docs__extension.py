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
    """扫描目录"""
    if not os.path.exists(DOCS_DIR):
        os.makedirs(DOCS_DIR)
    files = [f[:-3] for f in os.listdir(DOCS_DIR) if f.endswith('.md')]
    return jsonify({"docs": sorted(files)})


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