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
NEW_VIDEO_DIR = r'\\UGREEN-1E55\xin_Y\视频\upcloud-sex\all-data'
NEW_VIDEO_DIR2 = r'\\UGREEN-1E55\xin_Y\videospro'
# 🌟 新增：小说目录配置 (请根据实际 NAS 路径修改)
NOVEL_DIR = r'\\UGREEN-1E55\xin_Y\小说' 

# 内存索引字典
song_index = {}
video_index = {}
novel_index = {} # 🌟 新增：小说内存索引

# 支持的媒体格式
AUDIO_EXTS = ('.mp3', '.flac', '.wav', '.m4a', '.ogg')
VIDEO_EXTS = ('.mp4', '.mov', '.webm', '.mkv', '.avi')
NOVEL_EXTS = ('.txt', '.md', '.epub') # 🌟 新增：小说格式支持

# ================= 核心扫描引擎 =================
def scan_directory():
    """扫描目录并更新媒体索引"""
    global song_index, video_index, novel_index
    song_index.clear()
    video_index.clear()
    novel_index.clear() # 🌟 清理旧索引
    
    # 1. 扫描音频
    if os.path.exists(MUSIC_DIR):
        for root, _, files in os.walk(MUSIC_DIR):
            for file in files:
                if file.lower().endswith(AUDIO_EXTS):
                    song_index[file] = os.path.join(root, file)
                    
    # 2. 扫描默认视频
    if os.path.exists(VIDEO_DIR):
        for root, _, files in os.walk(VIDEO_DIR):
            if 'Deleted_Trash' in root or 'Liked_Favorites' in root: continue
            for file in files:
                if file.lower().endswith(VIDEO_EXTS):
                    video_index[file] = os.path.join(root, file)
                    
    # 3. 扫描新资源视频1
    if os.path.exists(NEW_VIDEO_DIR):
        for root, _, files in os.walk(NEW_VIDEO_DIR):
            if 'Deleted_Trash' in root or 'Liked_Favorites' in root: continue
            for file in files:
                if file.lower().endswith(VIDEO_EXTS):
                    prefixed_name = f"[NEW]_{file}"
                    video_index[prefixed_name] = os.path.join(root, file)
                    
    # 4. 扫描新资源视频2
    if os.path.exists(NEW_VIDEO_DIR2):
        for root, _, files in os.walk(NEW_VIDEO_DIR2):
            if 'Deleted_Trash' in root or 'Liked_Favorites' in root: continue
            for file in files:
                if file.lower().endswith(VIDEO_EXTS):
                    prefixed_name = f"[NEW2]_{file}"
                    video_index[prefixed_name] = os.path.join(root, file)
                    
    # 5. 🌟 扫描小说资源
    if os.path.exists(NOVEL_DIR):
        for root, _, files in os.walk(NOVEL_DIR):
            for file in files:
                if file.lower().endswith(NOVEL_EXTS):
                    novel_index[file] = os.path.join(root, file)
                    
    return len(song_index), len(video_index), len(novel_index)

# ================= API 路由 =================

@app.route('/api/scan', methods=['GET', 'POST'])
def trigger_scan():
    """手动触发全量重新扫描"""
    songs_count, videos_count, novels_count = scan_directory()
    return jsonify({
        "status": "success", 
        "message": "索引更新完毕", 
        "total_songs": songs_count,
        "total_videos": videos_count,
        "total_novels": novels_count # 🌟 响应中增加小说数量
    })

@app.route('/api/songs/json', methods=['GET'])
def get_songs_json():
    return jsonify(list(song_index.keys()))

@app.route('/api/videos/json', methods=['GET'])
def get_videos_json():
    return jsonify(list(video_index.keys()))

# 🌟 新增：获取小说列表
@app.route('/api/novels/json', methods=['GET'])
def get_novels_json():
    """返回所有小说的列表"""
    return jsonify(list(novel_index.keys()))

# 🌟 新增：获取特定小说的具体内容
@app.route('/api/novel/content/<path:novel_name>', methods=['GET'])
def get_novel_content(novel_name):
    """读取小说内容并返回，自动处理中文编码"""
    file_path = novel_index.get(novel_name)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Novel file not found"}), 404
        
    # 如果是epub等二进制文件，建议走下载流，这里主要针对txt/md文本
    if file_path.lower().endswith('.epub'):
        return send_file(file_path, as_attachment=True)

    content = ""
    # 核心逻辑：尝试以 utf-8 读取，如果遇到解码错误降级使用 gbk 
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(file_path, 'r', encoding='gbk', errors='replace') as f:
                content = f.read()
        except Exception as e:
            return jsonify({"error": f"Failed to read file encoding: {str(e)}"}), 500
            
    return jsonify({
        "title": novel_name,
        "content": content
    })

@app.route('/api/media/csv', methods=['GET'])
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Type', 'File Name', 'Local File Path'])
    
    for name, path in song_index.items():
        writer.writerow(['Audio', name, path])
    for name, path in video_index.items():
        writer.writerow(['Video', name, path])
    for name, path in novel_index.items():
        writer.writerow(['Novel', name, path]) # 🌟 CSV导出包含小说
        
    response = Response(output.getvalue(), mimetype='text/csv')
    response.headers['Content-Disposition'] = 'attachment; filename=media_index.csv'
    return response

# ================= 媒体流传输路由 =================
@app.route('/stream/<path:song_name>', methods=['GET'])
def stream_audio(song_name):
    file_path = song_index.get(song_name)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Audio file not found"}), 404
    return send_file(file_path, conditional=True)

@app.route('/stream/video/<path:video_name>', methods=['GET'])
def stream_video(video_name):
    file_path = video_index.get(video_name)
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "Video file not found"}), 404
    return send_file(file_path, conditional=True)

if __name__ == '__main__':
    # 路径检查
    if not os.path.exists(MUSIC_DIR): print(f"⚠️ 警告: 音乐目录 {MUSIC_DIR} 不存在或未挂载")
    if not os.path.exists(VIDEO_DIR): print(f"⚠️ 警告: 视频目录 {VIDEO_DIR} 不存在或未挂载")
    if not os.path.exists(NEW_VIDEO_DIR): print(f"⚠️ 警告: 新视频目录 {NEW_VIDEO_DIR} 不存在或未挂载")
    if not os.path.exists(NOVEL_DIR): print(f"⚠️ 警告: 小说目录 {NOVEL_DIR} 不存在或未挂载")
    
    s_count, v_count, n_count = scan_directory()
    print(f"✅ 资源节点启动成功！已建立 {s_count} 歌曲, {v_count} 视频, {n_count} 小说的索引。")
    
    app.run(host='0.0.0.0', port=8100, threaded=True)