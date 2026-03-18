import os
import requests
import urllib3
import librosa
import numpy as np

# 禁用自签证书导致的 HTTPS 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= 配置区 =================
LOCAL_MUSIC_DIR = './my_music'  # 你的本地歌曲文件夹路径
API_BASE_URL = 'https://apple4.zin6.dpdns.org:8100'
SUPPORTED_FORMATS = ('.mp3', '.flac', '.wav', '.m4a')
# ==========================================

def analyze_audio_dimensions(filepath):
    """
    仅提取基础声学维度，不进行场景推测。
    返回结构化标签，如: ['BPM:125', '能量:高', '起伏:平稳', '节拍:强']
    """
    tags = []
    try:
        # 只截取中间 30 秒，极大节省算力
        duration = 30
        y_temp, sr_temp = librosa.load(filepath, sr=None, duration=1) 
        total_time = librosa.get_duration(filename=filepath)
        offset = max(0, (total_time - duration) / 2)

        # 重采样到 22050Hz 加速计算
        y, sr = librosa.load(filepath, sr=22050, offset=offset, duration=duration)
        
        # 1. 提取精准 BPM
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = int(tempo[0] if isinstance(tempo, np.ndarray) else tempo)
        tags.append(f"BPM:{bpm}")
        
        # 2. 提取 RMS 能量均值与方差
        rms = librosa.feature.rms(y=y)[0]
        energy_mean = np.mean(rms)
        energy_std = np.std(rms)
        
        # 3. 提取节拍强度 (Onset Strength)
        onset_env = librosa.onset.onset_strength(y=y, sr=sr)
        onset_mean = np.mean(onset_env)
        
        # --- 基础维度转换逻辑 ---
        
        # 能量均值维度
        if energy_mean > 0.15:
            tags.append("能量:高")
        elif energy_mean > 0.05:
            tags.append("能量:中")
        else:
            tags.append("能量:低")
            
        # 能量起伏维度 (动态范围)
        if energy_std > 0.05:
            tags.append("起伏:大")
        elif energy_std > 0.02:
            tags.append("起伏:中")
        else:
            tags.append("起伏:平稳")
            
        # 节拍强度维度
        # 阈值 1.2 是一个经验值，根据 librosa 的输出特性，大于此值通常鼓点/节奏极强
        if onset_mean > 1.2:
            tags.append("节拍:强")
        else:
            tags.append("节拍:弱")

        return tags
        
    except Exception as e:
        print(f"⚠️ 分析文件跳过 {os.path.basename(filepath)}: {e}")
        return []

def scan_and_sync_dimensions():
    print(f"📡 正在连接服务器获取最新歌曲列表... ({API_BASE_URL})")
    try:
        resp = requests.get(f"{API_BASE_URL}/api/proxy/songs", verify=False, timeout=10)
        resp.raise_for_status()
        server_songs = resp.json()
    except Exception as e:
        print(f"❌ 获取服务器歌曲列表失败: {e}")
        return

    print(f"🎵 服务器当前共有 {len(server_songs)} 首歌曲。")
    server_stem_map = {os.path.splitext(song)[0]: song for song in server_songs}
    
    success_count = 0
    print(f"🔍 开始扫描并提取基础声学维度: {LOCAL_MUSIC_DIR}")
    
    for root, _, files in os.walk(LOCAL_MUSIC_DIR):
        for file in files:
            if not file.lower().endswith(SUPPORTED_FORMATS):
                continue
                
            local_stem = os.path.splitext(file)[0]
            
            if local_stem in server_stem_map:
                server_song_name = server_stem_map[local_stem]
                filepath = os.path.join(root, file)
                
                print(f"▶️ 提取维度: {local_stem}...")
                dimension_tags = analyze_audio_dimensions(filepath)
                
                if not dimension_tags:
                    continue
                
                payload = {
                    "song_name": server_song_name,
                    "tags": dimension_tags
                }
                
                try:
                    res = requests.post(f"{API_BASE_URL}/api/tags", json=payload, verify=False, timeout=5)
                    if res.status_code == 200:
                        print(f"   ✅ 同步成功 | {dimension_tags}")
                        success_count += 1
                    else:
                        print(f"   ❌ API 拒绝更新 | 状态码: {res.status_code}")
                except Exception as e:
                    print(f"   ❌ 网络请求异常: {e}")

    print(f"\n🎉 批量分析与同步完成！成功为 {success_count} 首歌曲提取并同步了基础维度。")

if __name__ == '__main__':
    scan_and_sync_dimensions()