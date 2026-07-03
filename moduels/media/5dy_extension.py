import os
import zipfile
import io
import mimetypes
from functools import lru_cache
from flask import Flask, Blueprint, request, jsonify, send_file

# ================= 本地资源配置 =================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ZIPS_DIR = os.path.join(CURRENT_DIR, "zips")
VALID_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}

# ================= 全局内存状态 =================
# 内存索引: { "filename.zip": {"images": [...], "count": N, "mtime": timestamp} }
LIBRARY_INDEX = {}

# ================= 核心业务逻辑 =================
def build_index():
    """扫描目录并建立 ZIP 结构内存索引"""
    LIBRARY_INDEX.clear()
    if not os.path.exists(ZIPS_DIR):
        os.makedirs(ZIPS_DIR)
        
    for file in os.listdir(ZIPS_DIR):
        if file.lower().endswith('.zip'):
            zip_path = os.path.join(ZIPS_DIR, file)
            try:
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    # 仅读取中央目录，过滤出图片文件
                    images = [
                        name for name in zf.namelist() 
                        if os.path.splitext(name)[1].lower() in VALID_EXTENSIONS
                    ]
                    mtime = os.path.getmtime(zip_path)
                    LIBRARY_INDEX[file] = {
                        "images": images,
                        "count": len(images),
                        "mtime": mtime
                    }
            except zipfile.BadZipFile:
                continue

@lru_cache(maxsize=256)
def get_image_bytes(zip_filename, image_path):
    """从 ZIP 中读取单张图片，不落地，使用 LRU 内存缓存"""
    zip_path = os.path.join(ZIPS_DIR, zip_filename)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        return zf.read(image_path)

# ================= 蓝图定义 =================
# 创建 API 蓝图并指定统一的 URL 前缀
api_bp = Blueprint('api', __name__, url_prefix='/api')

@api_bp.route('/library', methods=['GET'])
def get_library():
    """返回所有 ZIP 列表信息"""
    result = []
    for zip_name, data in LIBRARY_INDEX.items():
        result.append({
            "zip_name": zip_name,
            "count": data["count"],
            "mtime": data["mtime"]
        })
    result.sort(key=lambda x: x['mtime'], reverse=True)
    return jsonify(result)

@api_bp.route('/list', methods=['GET'])
def get_list():
    """返回特定 ZIP 内的图片列表"""
    zip_name = request.args.get('zip')
    if zip_name in LIBRARY_INDEX:
        return jsonify(LIBRARY_INDEX[zip_name]["images"])
    return jsonify({"error": "ZIP not found"}), 404

@api_bp.route('/search', methods=['GET'])
def search_images():
    """全局文件名模糊搜索"""
    query = request.args.get('q', '').lower()
    if not query:
        return jsonify([])
        
    results = []
    for zip_name, data in LIBRARY_INDEX.items():
        for img_path in data["images"]:
            if query in os.path.basename(img_path).lower():
                results.append({
                    "zip_name": zip_name,
                    "path": img_path
                })
    return jsonify(results)

@api_bp.route('/image', methods=['GET'])
def serve_image():
    """直接读取并返回图片二进制流"""
    zip_name = request.args.get('zip')
    image_path = request.args.get('path')
    
    if not zip_name or not image_path or zip_name not in LIBRARY_INDEX:
        return "Not found", 404
        
    try:
        img_bytes = get_image_bytes(zip_name, image_path)
        mime_type, _ = mimetypes.guess_type(image_path)
        return send_file(
            io.BytesIO(img_bytes),
            mimetype=mime_type or 'application/octet-stream'
        )
    except Exception as e:
        return str(e), 500

# ================= 应用初始化 =================
app = Flask(__name__)

# 注册蓝图
app.register_blueprint(api_bp)

@app.route('/')
def index():
    """返回单页面前端"""
    index_path = os.path.join(CURRENT_DIR, 'index.html')
    if os.path.exists(index_path):
        return send_file(index_path)
    return "index.html not found in current directory.", 404

if __name__ == '__main__':
    build_index()
    # 禁用模板重载避免重复扫描
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)