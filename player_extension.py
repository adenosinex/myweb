# player_extension.py
from flask import Blueprint, request, jsonify, stream_with_context, Response
import requests

# 创建一个名为 'player' 的蓝图
player_bp = Blueprint('player', __name__)

# 配置资源节点地址 (局域网源站)
RESOURCE_NODE_URL = "http://192.168.31.204:8100"

@player_bp.route('/api/proxy/songs', methods=['GET'])
def proxy_songs():
    url = f"{RESOURCE_NODE_URL}/api/songs/json"
    print(f"\n[调试信息] 正在由主后端向资源节点发起请求: {url}")
    try:
        # 强制不使用代理，避免网络层拦截
        resp = requests.get(url, timeout=10, proxies={"http": None, "https": None})
        
        if resp.status_code != 200:
            print(f"[错误] 资源节点返回了非 200 状态，内容为: {resp.text[:200]}")
            return jsonify({"error": f"资源节点报错: {resp.status_code}"}), 500
            
        data = resp.json()
        print(f"[调试信息] 成功解析 JSON，共获取到 {len(data)} 首歌曲")
        return jsonify(data)
        
    except requests.exceptions.Timeout:
        return jsonify({"error": "连接超时", "details": "Timeout"}), 500
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": "网络不通或连接被拒绝", "details": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "代理请求发生致命错误", "details": str(e)}), 500


@player_bp.route('/stream/<path:song_name>', methods=['GET'])
def proxy_stream(song_name):
    url = f"{RESOURCE_NODE_URL}/stream/{song_name}"
    headers = {key: value for (key, value) in request.headers if key.lower() != 'host'}
    try:
        req = requests.get(url, headers=headers, stream=True, proxies={"http": None, "https": None})
        excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
        resp_headers = [(name, value) for (name, value) in req.raw.headers.items() if name.lower() not in excluded_headers]
        
        return Response(stream_with_context(req.iter_content(chunk_size=1024 * 1024)),
                        status=req.status_code, 
                        headers=resp_headers)
    except Exception as e:
        return jsonify({"error": "音频流转发失败"}), 500