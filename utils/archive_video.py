import os
import shutil
import requests
import csv

# ==========================================
# ⚙️ 客户端配置区域
# ==========================================
# Mac mini 的 API 地址
MAC_MINI_API_URL = "http://apple4.zin6.dpdns.org:8100" 

# 资源服务器上的本地视频根目录 (SMB 路径)
VIDEO_BASE_DIR = r'\\UGREEN-1E55\xin_Y\视频\抖音' 

# 集中存放的新文件夹名称
LIKED_DIR = os.path.join(VIDEO_BASE_DIR, '2025/Liked_Favorites')
DELETED_DIR = os.path.join(VIDEO_BASE_DIR, 'Deleted_Trash')

# 缓存文件名称
CACHE_CSV = 'file_index_cache.csv'

def build_cache(base_dir, cache_file):
    """遍历目录，建立文件路径缓存并存为 CSV"""
    print("🔍 正在扫描 NAS 文件目录，建立本地文件路径缓存 (这可能需要几分钟)...")
    file_map = {}
    
    # os.walk 会遍历 base_dir 下的所有子文件夹
    for root, dirs, files in os.walk(base_dir):
        # 排除已归档的文件夹，避免重复扫描已经移动进去的文件
        if LIKED_DIR in root or DELETED_DIR in root:
            continue
            
        for f in files:
            # 以文件名为 key，绝对路径为 value
            file_map[f] = os.path.join(root, f)
            
    # 将结果写入 CSV
    try:
        with open(cache_file, 'w', encoding='utf-8', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['filename', 'filepath'])  # 写入表头
            for fname, fpath in file_map.items():
                writer.writerow([fname, fpath])
        print(f"✅ 缓存建立完成，共索引了 {len(file_map)} 个文件，已保存至 {cache_file}。")
    except Exception as e:
        print(f"⚠️ 缓存写入失败: {e}")
        
    return file_map

def load_cache(base_dir, cache_file):
    """读取 CSV 缓存，如果不存在则调用 build_cache 建立"""
    file_map = {}
    if os.path.exists(cache_file):
        print(f"📦 发现本地缓存 [{cache_file}]，正在直接读取...")
        try:
            with open(cache_file, 'r', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                next(reader, None)  # 跳过表头
                for row in reader:
                    if len(row) == 2:
                        file_map[row[0]] = row[1]
            print(f"✅ 从缓存中快速加载了 {len(file_map)} 个文件路径。")
        except Exception as e:
            print(f"⚠️ 缓存读取异常 ({e})，将重新扫描...")
            file_map = build_cache(base_dir, cache_file)
    else:
        file_map = build_cache(base_dir, cache_file)
        
    return file_map

def get_source_path(fname, file_map):
    """从缓存中获取源路径，增加失效降级机制"""
    src_path = file_map.get(fname)
    # 如果缓存里有记录且文件确实存在，直接返回
    if src_path and os.path.exists(src_path):
        return src_path
    
    # 降级：如果缓存失效(比如新加的视频还没缓存)，尝试直接在根目录找一下
    guess_path = os.path.join(VIDEO_BASE_DIR, fname)
    if os.path.exists(guess_path):
        return guess_path
        
    return None

def main():
    os.makedirs(LIKED_DIR, exist_ok=True)
    os.makedirs(DELETED_DIR, exist_ok=True)

    # 1. 初始化文件缓存字典
    file_map = load_cache(VIDEO_BASE_DIR, CACHE_CSV)

    print("2. 📡 正在向大脑 (Mac mini) 请求待处理视频名单...")
    try:
        resp = requests.get(f"{MAC_MINI_API_URL}/api/video/maintenance/list", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"❌ 无法连接到 Mac mini API: {e}")
        input("按任意键退出...")
        return

    liked_files = data.get("liked", [])
    deleted_files = data.get("deleted", [])

    print(f"📊 获取成功：待归档【喜欢】视频 {len(liked_files)} 个，待归档【删除】视频 {len(deleted_files)} 个。")

    # ==========================================
    # 3. 处理【喜欢】的视频
    # ==========================================
    liked_moved = 0
    for fname in liked_files:
        dst_path = os.path.join(LIKED_DIR, fname)
        if os.path.exists(dst_path):
            continue  # 已经在目标位置，跳过
            
        src_path = get_source_path(fname, file_map)
        if src_path:
            try:
                shutil.move(src_path, dst_path)
                liked_moved += 1
            except Exception as e:
                print(f"⚠️ 移动喜欢视频 [{fname}] 失败: {e}")

    # ==========================================
    # 4. 处理【删除】的视频，并记录成功列表
    # ==========================================
    deleted_moved_success = []
    for fname in deleted_files:
        dst_path = os.path.join(DELETED_DIR, fname)
        
        # 严格检查 1：如果目标文件夹里已经有这个文件了，说明移动到位了，允许删除记录
        if os.path.exists(dst_path):
            deleted_moved_success.append(fname)
            continue
            
        # 严格检查 2：获取源文件路径进行移动
        src_path = get_source_path(fname, file_map)
        if src_path:
            try:
                shutil.move(src_path, dst_path)
                deleted_moved_success.append(fname) # 只有这里发生移动且没报错，才加入成功列表
            except Exception as e:
                print(f"⚠️ 移动删除视频 [{fname}] 失败: {e}")
        else:
            # 严格检查 3：如果缓存找不到，根目录也找不到，坚决不向服务器发删除指令
            print(f"⚠️ 找不到视频 [{fname}] 的物理文件，取消清理该记录。")

    print(f"📁 物理操作完成：移动了 {liked_moved} 个喜欢视频，成功处理了 {len(deleted_moved_success)} 个删除视频。")

    # ==========================================
    # 5. 通知 Mac mini 清理数据库记录
    # ==========================================
    if deleted_moved_success:
        print(f"5. 🧹 正在通知大脑彻底清理 {len(deleted_moved_success)} 条已归位垃圾记录...")
        try:
            del_resp = requests.post(
                f"{MAC_MINI_API_URL}/api/video/maintenance/confirm_delete",
                json={"filenames": deleted_moved_success},
                timeout=10
            )
            del_resp.raise_for_status()
            res_data = del_resp.json()
            print(f"✨ 数据库清理完成！大脑确认已删除 {res_data.get('deleted_count', 0)} 条记录。")
        except Exception as e:
            print(f"❌ 通知清理失败: {e}")
    else:
        print("✨ 没有满足物理移动条件的记录需要清理。")
        
    input("end:")

if __name__ == '__main__':
    main()