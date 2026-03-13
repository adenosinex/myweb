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
VIDEO_DIR = r'\\UGREEN-1E55\xin_Y\视频\抖音\2025' 
# 🌟 统一命名规范
NEW_VIDEO_DIR = r'\\UGREEN-1E55\xin_Y\视频\upcloud-sex\all-data'
NEW_VIDEO_DIR2 = r'\\UGREEN-1E55\xin_Y\videospro'

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
                    
    # 2. 扫描默认视频
    if os.path.exists(VIDEO_DIR):
        for root, _, files in os.walk(VIDEO_DIR):
            # 排除可能被归档脚本创建的垃圾桶文件夹
            if 'Deleted_Trash' in root or 'Liked_Favorites' in root: continue
            for file in files:
                if file.lower().endswith(VIDEO_EXTS):
                    video_index[file] = os.path.join(root, file)
                    
    # 3. 🌟 扫描新资源视频 (添加虚拟前缀)
    if os.path.exists(NEW_VIDEO_DIR):
        for root, _, files in os.walk(NEW_VIDEO_DIR):
            if 'Deleted_Trash' in root or 'Liked_Favorites' in root: continue
            for file in files:
                if file.lower().endswith(VIDEO_EXTS):
                    # 核心魔法：字典的 Key 加上前缀，Value 依然是真实的物理路径
                    prefixed_name = f"[NEW]_{file}"
                    video_index[prefixed_name] = os.path.join(root, file)
    # 3. 🌟 扫描新资源视频 (添加虚拟前缀)
    if os.path.exists(NEW_VIDEO_DIR2):
        for root, _, files in os.walk(NEW_VIDEO_DIR2):
            if 'Deleted_Trash' in root or 'Liked_Favorites' in root: continue
            for file in files:
                if file.lower().endswith(VIDEO_EXTS):
                    # 核心魔法：字典的 Key 加上前缀，Value 依然是真实的物理路径
                    prefixed_name = f"[NEW2]_{file}"
                    video_index[prefixed_name] = os.path.join(root, file)
                    
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
    # 🌟 这里直接用带 [NEW]_ 前缀的名字去查字典，能完美拿到真实的无前缀物理路径
    file_path = video_index.get(video_name)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Video file not found"}), 404
    return send_file(file_path, conditional=True)

if __name__ == '__main__':
    if not os.path.exists(MUSIC_DIR): print(f"⚠️ 警告: 音乐目录 {MUSIC_DIR} 不存在或未挂载")
    if not os.path.exists(VIDEO_DIR): print(f"⚠️ 警告: 视频目录 {VIDEO_DIR} 不存在或未挂载")
    if not os.path.exists(NEW_VIDEO_DIR): print(f"⚠️ 警告: 新视频目录 {NEW_VIDEO_DIR} 不存在或未挂载")
    
    s_count, v_count = scan_directory()
    print(f"✅ 资源节点启动成功！已建立 {s_count} 首歌曲, {v_count} 个视频的索引。")
    
    app.run(host='0.0.0.0', port=8100, threaded=True)