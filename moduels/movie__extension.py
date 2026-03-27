import os
import requests
from flask import Blueprint, request, jsonify, render_template, Response

# 定义蓝图
gallery_bp = Blueprint('gallery', __name__)

# 配置实际资源节点的地址
RESOURCE_NODE_URL = os.getenv('RESOURCE_NODE_URL', 'http://15x4.zin6.dpdns.org:5900')

# ================= 接口路由 =================

@gallery_bp.route('/gallery', methods=['GET'])
def view_gallery():
    """提供图库前端页面的访问点"""
    return render_template('gallery.html')


@gallery_bp.route('/gallery/api/frames', methods=['GET'])
def proxy_frames():
    """
    蓝图代理接口：
    接收前端请求，将其转发给远程真实资源节点。
    重写图片资源的 URL，让前端向当前蓝图发起图片请求。
    """
    try:
        params = request.args.to_dict()
        target_url = f"{RESOURCE_NODE_URL.rstrip('/')}/api/frames"
        
        resp = requests.get(target_url, params=params, timeout=10)
        
        if resp.status_code == 200:
            data = resp.json()
            
            # 核心：修改 JSON 里的资源路径，指向蓝图的图片转发接口
            if 'data' in data and isinstance(data['data'], list):
                for item in data['data']:
                    orig_path = item.get('original_url', '')
                    thumb_path = item.get('thumbnail_url', '')
                    
                    if orig_path.startswith('/view_original/'):
                        filename = orig_path.replace('/view_original/', '')
                        item['original_url'] = f"/gallery/view_original/{filename}"
                    
                    if thumb_path.startswith('/thumb/'):
                        fid = thumb_path.replace('/thumb/', '')
                        item['thumbnail_url'] = f"/gallery/thumb/{fid}"
                        
            return jsonify(data)
        else:
            return jsonify({
                "code": resp.status_code, 
                "msg": f"资源节点返回异常: {resp.text}"
            }), 502
            
    except requests.exceptions.RequestException as e:
        return jsonify({
            "code": 500, 
            "msg": f"无法连接到资源节点 ({RESOURCE_NODE_URL}): {str(e)}"
        }), 500


@gallery_bp.route('/gallery/view_original/<path:filename>', methods=['GET'])
def proxy_view_original(filename):
    """
    图片转发接口：高清原图
    流式读取资源节点的数据并透传给前端，防止大图片导致内存溢出
    """
    target_url = f"{RESOURCE_NODE_URL.rstrip('/')}/view_original/{filename}"
    try:
        resp = requests.get(target_url, stream=True, timeout=15)
        if resp.status_code == 200:
            return Response(
                resp.iter_content(chunk_size=8192), 
                status=resp.status_code, 
                content_type=resp.headers.get('Content-Type')
            )
        return "原图获取失败", resp.status_code
    except requests.exceptions.RequestException:
        return "原图代理加载超时或失败", 502


@gallery_bp.route('/gallery/thumb/<int:fid>', methods=['GET'])
def proxy_thumb(fid):
    """
    图片转发接口：缩略图
    """
    target_url = f"{RESOURCE_NODE_URL.rstrip('/')}/thumb/{fid}"
    try:
        resp = requests.get(target_url, stream=True, timeout=10)
        if resp.status_code == 200:
            return Response(
                resp.iter_content(chunk_size=8192), 
                status=resp.status_code, 
                content_type=resp.headers.get('Content-Type')
            )
        return "缩略图获取失败", resp.status_code
    except requests.exceptions.RequestException:
        return "缩略图代理加载超时或失败", 502