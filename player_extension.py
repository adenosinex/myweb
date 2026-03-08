# player_extension.py
from flask import Blueprint, request, jsonify, stream_with_context, Response
import requests
import sqlite3
import time  # 修复 1：导入 time 模块

# 创建一个名为 'player' 的蓝图
player_bp = Blueprint('player', __name__)

# 配置资源节点地址 (局域网源站)
RESOURCE_NODE_URL = "http://192.168.31.204:8100"
# 修复 2：补充数据库路径常量
DB_PATH = 'universal_data.db' 

@player_bp.route('/api/play_stats', methods=['GET'])
def get_play_stats():
    """初始化时给前端下发权威的统计数据"""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # 确保如果表刚建好里面没数据时不报错
        try:
            cursor.execute('SELECT song_name, accumulated_time, recent_skip_count, last_played_at FROM play_stats')
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            return jsonify({})
        
    stats = {}
    for row in rows:
        stats[row[0]] = {
            "accumulatedTime": row[1],
            "recentSkipCount": row[2],
            "lastPlayedAt": row[3]
        }
    return jsonify(stats)

@player_bp.route('/api/play_stats/sync', methods=['POST'])
def sync_play_stats():
    """接收前端离线队列的批量汇报并合并"""
    events = request.json  # 这是一个列表，包含多条听歌事件
    if not events:
        return jsonify({"status": "success"})
        
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for event in events:
            song = event.get('song_name')
            played_time = event.get('played_time', 0)
            is_skip = event.get('is_skip', False)
            is_complete = event.get('is_complete', False)
            timestamp = event.get('timestamp', int(time.time() * 1000))

            # 提取旧数据
            cursor.execute('SELECT accumulated_time, recent_skip_count FROM play_stats WHERE song_name=?', (song,))
            row = cursor.fetchone()

            if row:
                acc_time = row[0] + played_time
                skip_count = row[1]
                
                if is_skip:
                    skip_count += 1
                elif is_complete:
                    skip_count = 0  # 听完洗白惩罚
                elif played_time > 60:
                    skip_count = max(0, skip_count - 1) # 听过半减刑

                cursor.execute('''
                    UPDATE play_stats
                    SET accumulated_time=?, recent_skip_count=?, last_played_at=?
                    WHERE song_name=?
                ''', (acc_time, skip_count, timestamp, song))
            else:
                skip_count = 1 if is_skip else 0
                cursor.execute('''
                    INSERT INTO play_stats (song_name, accumulated_time, recent_skip_count, last_played_at)
                    VALUES (?, ?, ?, ?)
                ''', (song, played_time, skip_count, timestamp))
                
    return jsonify({"status": "success"})

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