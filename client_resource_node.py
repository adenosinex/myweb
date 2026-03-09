from flask import Flask, Response, send_file, jsonify
from flask_cors import CORS
import os
import csv
import io

app = Flask(__name__)
# 解决跨域问题，允许前端/主后端直接请求流
CORS(app) 

# ================= 配置区 =================
MUSIC_DIR = r'\\UGREEN-1E55\xin_Y\音乐'
# 新增：配置你的视频文件夹路径 (请根据实际情况修改)
VIDEO_DIR = r'\\One\d\move video\work' 

# 内存索引字典
song_index = {}
video_index = {}

# 支持的媒体格式
AUDIO_EXTS = ('.mp3', '.flac', '.wav', '.m4a', '.ogg')
VIDEO_EXTS = ('.mp4', '.mov', '.webm', '.mkv', '.avi')

# ================= 核心扫描引擎 =================
def scan_directory():
    """扫描目录并更新音频和视频索引"""
    global song_index, video_index
    song_index.clear()
    video_index.clear()
    
    # 1. 扫描音频
    if os.path.exists(MUSIC_DIR):
        for root, _, files in os.walk(MUSIC_DIR):
            for file in files:
                if file.lower().endswith(AUDIO_EXTS):
                    song_index[file] = os.path.join(root, file)
                    
    # 2. 扫描视频
    if os.path.exists(VIDEO_DIR):
        for root, _, files in os.walk(VIDEO_DIR):
            for file in files:
                if file.lower().endswith(VIDEO_EXTS):
                    video_index[file] = os.path.join(root, file)
                    
    return len(song_index), len(video_index)

# ================= API 路由 =================

@app.route('/api/scan', methods=['GET', 'POST'])
def trigger_scan():
    """手动触发全量重新扫描"""
    songs_count, videos_count = scan_directory()
    return jsonify({
        "status": "success", 
        "message": "索引更新完毕", 
        "total_songs": songs_count,
        "total_videos": videos_count
    })

@app.route('/api/songs/json', methods=['GET'])
def get_songs_json():
    """返回所有歌曲的列表 (供主后端 proxy 使用)"""
    return jsonify(list(song_index.keys()))

@app.route('/api/videos/json', methods=['GET'])
def get_videos_json():
    """返回所有视频的列表"""
    return jsonify(list(video_index.keys()))

@app.route('/api/media/csv', methods=['GET'])
def export_csv():
    """将当前的音视频索引导出为 CSV"""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Type', 'File Name', 'Local File Path'])
    
    for name, path in song_index.items():
        writer.writerow(['Audio', name, path])
    for name, path in video_index.items():
        writer.writerow(['Video', name, path])
        
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=media_index.csv'
    return response

# ================= 媒体流传输路由 =================

@app.route('/stream/<path:song_name>', methods=['GET'])
def stream_audio(song_name):
    """音频流端点"""
    file_path = song_index.get(song_name)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Audio file not found"}), 404
    return send_file(file_path, conditional=True)

@app.route('/stream/video/<path:video_name>', methods=['GET'])
def stream_video(video_name):
    """视频流端点 (支持 Range 请求，随意拖拽)"""
    file_path = video_index.get(video_name)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Video file not found"}), 404
    # send_file 配合 conditional=True 可以完美处理视频的 206 Partial Content
    return send_file(file_path, conditional=True)

if __name__ == '__main__':
    # 确保媒体目录存在（不强求必须创建，防止网络盘未挂载时报错，只做检查）
    if not os.path.exists(MUSIC_DIR): print(f"⚠️ 警告: 音乐目录 {MUSIC_DIR} 不存在或未挂载")
    if not os.path.exists(VIDEO_DIR): print(f"⚠️ 警告: 视频目录 {VIDEO_DIR} 不存在或未挂载")
    
    # 启动时先扫描一次
    s_count, v_count = scan_directory()
    print(f"✅ 资源节点启动成功！已建立 {s_count} 首歌曲, {v_count} 个视频的索引。")
    
    # 运行在 8100 端口
    app.run(host='0.0.0.0', port=8100, threaded=True)