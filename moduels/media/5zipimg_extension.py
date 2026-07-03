import os
import requests
from flask import Flask, Blueprint, request, jsonify, Response, stream_with_context, send_file

# ================= 本地路径与节点配置 =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_FILE = os.path.join(CURRENT_DIR, 'index.html')

# 资源节点 (Resource Agent) 列表
# 后端不处理 ZIP，所有数据从此处的节点获取
AGENTS = [
    "http://192.168.31.124:8001",
    # 以后可以随时增加新节点，例如: "http://192.168.1.10:8001"
]
# ====================================================

# 创建 API 蓝图，统一指定前缀
api_bp = Blueprint('zipapi', __name__, url_prefix='/zipapi')

@api_bp.route('/library', methods=['GET'])
def get_library():
    """获取所有节点的 ZIP 列表并聚合"""
    all_zips = []
    for idx, agent_url in enumerate(AGENTS):
        try:
            # 向节点请求库信息
            resp = requests.get(f"{agent_url}/library", timeout=3)
            if resp.status_code == 200:
                for item in resp.json():
                    item['agent_idx'] = idx  # 注入节点索引，供前端后续定向请求使用
                    all_zips.append(item)
        except requests.RequestException:
            # 忽略离线或异常的节点
            continue
            
    # 按更新时间倒序排列
    all_zips.sort(key=lambda x: x.get('mtime', 0), reverse=True)
    return jsonify(all_zips)

@api_bp.route('/list', methods=['GET'])
def get_list():
    """获取指定 ZIP 内的图片列表"""
    agent_idx = request.args.get('agent', type=int, default=0)
    zip_name = request.args.get('zip')
    
    if agent_idx >= len(AGENTS):
        return jsonify({"error": "Invalid agent index"}), 400
        
    url = f"{AGENTS[agent_idx]}/list"
    try:
        resp = requests.get(url, params={"zip": zip_name}, timeout=3)
        return jsonify(resp.json()), resp.status_code
    except requests.RequestException:
        return jsonify({"error": "Resource agent unavailable"}), 502

@api_bp.route('/search', methods=['GET'])
def search_images():
    """全局搜索所有节点的图片"""
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
        
    all_results = []
    for idx, agent_url in enumerate(AGENTS):
        try:
            resp = requests.get(f"{agent_url}/search", params={"q": query}, timeout=5)
            if resp.status_code == 200:
                for item in resp.json():
                    item['agent_idx'] = idx # 注入节点索引
                    all_results.append(item)
        except requests.RequestException:
            continue
            
    return jsonify(all_results)

@api_bp.route('/image', methods=['GET'])
def serve_image():
    """代理转发图片二进制流（核心：不产生临时文件，流式传输极低内存占用）"""
    agent_idx = request.args.get('agent', type=int, default=0)
    zip_name = request.args.get('zip')
    image_path = request.args.get('path')
    
    if agent_idx >= len(AGENTS):
        return "Invalid agent index", 400

    url = f"{AGENTS[agent_idx]}/image"
    params = {'zip': zip_name, 'path': image_path}
    
    try:
        # 开启 stream=True，后端作为透明代理转发数据块，避免大图片吃满内存
        req = requests.get(url, params=params, stream=True, timeout=10)
        
        if req.status_code != 200:
            return "Image not found on agent", req.status_code
            
        return Response(
            stream_with_context(req.iter_content(chunk_size=8192)),
            content_type=req.headers.get('Content-Type', 'application/octet-stream')
        )
    except requests.RequestException:
        return "Failed to fetch image from agent", 502

# ================= 主程序初始化与监听 =================
app = Flask(__name__)

# 注册配置好的蓝图
app.register_blueprint(api_bp)

@app.route('/')
def index():
    """直接返回同级目录下的前端单页面"""
    if os.path.exists(INDEX_FILE):
        return send_file(INDEX_FILE)
    return "Error: index.html not found in current directory.", 404

if __name__ == '__main__':
    # 监听代码由主程序启动，蓝图仅作为模块存在
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)