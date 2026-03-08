import csv
import os
import requests
import urllib3

# 禁用自签证书导致的 HTTPS 警告（因为你是动态域名 dpdns，防止报错）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================= 配置区 =================
CSV_FILE = 'tags.csv'  # 你的旧标签数据文件
API_BASE_URL = 'https://apple4.zin6.dpdns.org:8100'
# ==========================================

def load_csv_tags(filepath):
    """读取 CSV 并以 stem (无后缀文件名) 为 Key 构建字典"""
    stem_map = {}
    if not os.path.exists(filepath):
        print(f"❌ 找不到文件: {filepath}")
        return stem_map
        
    # utf-8-sig 可以兼容带 BOM 的 Excel 导出格式
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        # 支持逗号或制表符(Tab)分割，兼容性更好
        # 假设文件格式: "歌曲名,分类标签" 或 "歌曲名\t分类标签"
        dialect = csv.Sniffer().sniff(f.read(1024), delimiters=[',', '\t'])
        f.seek(0)
        reader = csv.reader(f, dialect=dialect)
        
        next(reader, None)  # 跳过表头
        for row in reader:
            if len(row) < 2:
                continue
            song_name = row[0].strip()
            tags_str = row[1].strip()
            
            # 提取无后缀的 stem，例如 "晚风.mp3" -> "晚风"
            stem = os.path.splitext(song_name)[0]
            
            # 将 "说唱 / 电音 / DJ" 拆分成列表 ["说唱", "电音", "DJ"]
            tags_list = [t.strip() for t in tags_str.split('/') if t.strip()]
            
            if stem and tags_list:
                stem_map[stem] = tags_list
                
    print(f"📄 成功从 CSV 加载了 {len(stem_map)} 首歌曲的标签特征。")
    return stem_map

def sync_tags_to_server():
    stem_tags_map = load_csv_tags(CSV_FILE)
    if not stem_tags_map:
        return

    print(f"📡 正在连接服务器获取最新歌曲列表... ({API_BASE_URL})")
    try:
        # 获取当前服务器歌曲列表
        # 注意：你的 app.py 中拦截器配置了 path.startswith('/api/') 直接放行，所以不需要传 Cookie
        resp = requests.get(f"{API_BASE_URL}/api/proxy/songs", verify=False, timeout=10)
        resp.raise_for_status()
        server_songs = resp.json()
    except Exception as e:
        print(f"❌ 获取服务器歌曲列表失败: {e}")
        return

    print(f"🎵 服务器当前共有 {len(server_songs)} 首歌曲。开始进行 Stem 匹配与标签回写...\n")
    
    success_count = 0
    for server_song in server_songs:
        # 提取服务器歌曲的 stem
        server_stem = os.path.splitext(server_song)[0]
        
        # 如果旧数据中存在这个 stem，就执行更新
        if server_stem in stem_tags_map:
            tags = stem_tags_map[server_stem]
            payload = {
                "song_name": server_song,  # 使用服务器当前真实的带后缀全名
                "tags": tags
            }
            
            try:
                res = requests.post(f"{API_BASE_URL}/api/tags", json=payload, verify=False, timeout=5)
                if res.status_code == 200:
                    print(f"✅ 更新成功 | 匹配: [{server_stem}] -> 绑定到: {server_song} => {tags}")
                    success_count += 1
                else:
                    print(f"❌ 更新失败 | {server_song} | 状态码: {res.status_code}")
            except Exception as e:
                print(f"❌ 请求异常 | {server_song} | 错误: {e}")

    print(f"\n🎉 批量同步完成！成功为 {success_count} 首歌曲恢复/更新了标签。")

if __name__ == '__main__':
    sync_tags_to_server()