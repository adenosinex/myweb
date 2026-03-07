from flask import Flask, Response, send_file, jsonify
from flask_cors import CORS  # 解决跨域问题，允许前端播放音频
import os
import csv
import io

app = Flask(__name__)
CORS(app) # 允许主后端页面跨域请求本节点的音频流

# 配置你的音乐文件夹路径
MUSIC_DIR = r'\\UGREEN-1E55\xin_Y\音乐'
# 内存索引字典：{ "歌曲名.mp3": "/完整/绝对/路径/歌曲名.mp3" }
song_index = {}

def scan_directory():
    """扫描目录并更新索引"""
    global song_index
    song_index.clear()
    count = 0
    # 遍历目录及子目录
    for root, _, files in os.walk(MUSIC_DIR):
        for file in files:
            # 过滤常见的音频格式
            if file.lower().endswith(('.mp3', '.flac', '.wav', '.m4a')):
                full_path = os.path.join(root, file)
                # 以文件名为 Key，如果有同名文件，后扫描的会覆盖，可根据需求修改
                song_index[file] = full_path
                count += 1
    return count

@app.route('/api/scan', methods=['GET', 'POST'])
def trigger_scan():
    """手动触发全量重新扫描"""
    count = scan_directory()
    return jsonify({"status": "success", "message": f"索引更新完毕", "total_songs": count})

@app.route('/api/songs/csv', methods=['GET'])
def export_csv():
    """将当前索引导出为 CSV 并直接返回"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Song Name', 'Local File Path']) # 表头
    
    for name, path in song_index.items():
        writer.writerow([name, path])
    
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=songs_index.csv'
    return response

@app.route('/stream/<path:song_name>', methods=['GET'])
def stream_audio(song_name):
    """根据歌曲名获取流"""
    file_path = song_index.get(song_name)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    
    # send_file 自动处理 Range 请求，支持音视频流式播放和拖拽进度条
    return send_file(file_path, conditional=True)

if __name__ == '__main__':
    # 确保音乐目录存在
    os.makedirs(MUSIC_DIR, exist_ok=True)
    # 启动时先扫描一次
    count = scan_directory()
    print(f"✅ 资源节点启动成功！已建立 {count} 首歌曲的索引。")
    # 运行在 8080 端口，前端页面可向这里请求流
    app.run(host='0.0.0.0', port=8100, threaded=True)