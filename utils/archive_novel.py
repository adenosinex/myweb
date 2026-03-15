import os
from pathlib import Path
import shutil
import requests
import csv

# ==========================================
# ⚙️ 客户端配置区域
# ==========================================
# Mac mini 的 API 地址 (请确保端口正确)
MAC_MINI_API_URL = "http://apple4.zin6.dpdns.org:8100" 

# 资源服务器上的本地小说根目录 (SMB 路径，请根据实际情况修改)
NOVEL_BASE_DIR = r'\\Synology\home\sync od\funny\收藏小说\allstory' 

# 集中存放的新文件夹名称
LIKED_DIR = os.path.join(NOVEL_BASE_DIR, 'Liked_Favorites')
dp=Path(NOVEL_BASE_DIR).parent
DELETED_DIR = os.path.join(dp, 'Deleted_Trash')

# 缓存文件名称
CACHE_CSV = 'novel_index_cache.csv'

def build_cache(base_dir, cache_file):
    """遍历目录，建立小说文件路径缓存并存为 CSV"""
    print("🔍 正在扫描文件目录，建立本地小说路径缓存...")
    file_map = {}
    
    for root, dirs, files in os.walk(base_dir):
        # 排除已归档的文件夹
        if LIKED_DIR in root or DELETED_DIR in root:
            continue
            
        for f in files:
            if f.endswith('.txt'): # 只索引 txt 文件
                file_map[f] = os.path.join(root, f)
            
    try:
        with open(cache_file, 'w', encoding='utf-8', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['filename', 'filepath'])
            for fname, fpath in file_map.items():
                writer.writerow([fname, fpath])
        print(f"✅ 缓存建立完成，共索引了 {len(file_map)} 本小说。")
    except Exception as e:
        print(f"⚠️ 缓存写入失败: {e}")
        
    return file_map

def load_cache(base_dir, cache_file):
    """读取 CSV 缓存，如果不存在则建立"""
    file_map = {}
    if os.path.exists(cache_file):
        print(f"📦 发现本地缓存，正在直接读取...")
        try:
            with open(cache_file, 'r', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                next(reader, None) 
                for row in reader:
                    if len(row) == 2:
                        file_map[row[0]] = row[1]
            print(f"✅ 从缓存中加载了 {len(file_map)} 本小说路径。")
        except Exception as e:
            print(f"⚠️ 缓存读取异常 ({e})，将重新扫描...")
            file_map = build_cache(base_dir, cache_file)
    else:
        file_map = build_cache(base_dir, cache_file)
        
    return file_map

def get_source_path(fname, file_map):
    """获取源路径"""
    src_path = file_map.get(fname)
    if src_path and os.path.exists(src_path):
        return src_path
    
    # 降级：根目录直接查找
    guess_path = os.path.join(NOVEL_BASE_DIR, fname)
    if os.path.exists(guess_path):
        return guess_path
        
    return None

def main():
    os.makedirs(LIKED_DIR, exist_ok=True)
    os.makedirs(DELETED_DIR, exist_ok=True)

    file_map = load_cache(NOVEL_BASE_DIR, CACHE_CSV)

    print("\n2. 📡 正在向大脑 (Mac mini) 请求待处理小说名单...")
    try:
        resp = requests.get(f"{MAC_MINI_API_URL}/api/novel/maintenance/list", timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"❌ 无法连接到 Mac mini API: {e}")
        input("按任意键退出...")
        return

    liked_files = data.get("liked", [])
    deleted_files = data.get("deleted", [])

    print(f"📊 获取成功：待归档【喜欢】小说 {len(liked_files)} 本，待处理【隐藏/删除】小说 {len(deleted_files)} 本。")

    # ==========================================
    # 3. 处理【喜欢】的小说 (移动到收藏夹)
    # ==========================================
    liked_moved = 0
    for fname in liked_files:
        dst_path = os.path.join(LIKED_DIR, fname)
        if os.path.exists(dst_path):
            continue 
            
        src_path = get_source_path(fname, file_map)
        if src_path:
            try:
                shutil.move(src_path, dst_path)
                liked_moved += 1
            except Exception as e:
                print(f"⚠️ 移动喜欢书籍 [{fname}] 失败: {e}")

    # ==========================================
    # 4. 处理【删除/隐藏】的小说 (移动到回收站)
    # ==========================================
    deleted_moved_success = []
    for fname in deleted_files:
        dst_path = os.path.join(DELETED_DIR, fname)
        
        if os.path.exists(dst_path):
            deleted_moved_success.append(fname)
            continue
            
        src_path = get_source_path(fname, file_map)
        if src_path:
            try:
                shutil.move(src_path, dst_path)
                deleted_moved_success.append(fname) 
            except Exception as e:
                print(f"⚠️ 移动删除书籍 [{fname}] 失败: {e}")
        else:
            print(f"⚠️ 找不到书籍 [{fname}] 的物理文件，可能已删除。")
            # 如果物理文件真的没了，也算处理成功，通知服务器清理残余数据
            deleted_moved_success.append(fname)

    print(f"\n📁 物理操作完成：归档了 {liked_moved} 本喜欢书籍，移除了 {len(deleted_moved_success)} 本垃圾书籍。")

    # ==========================================
    # 5. 通知 Mac mini 清理垃圾数据库记录
    # ==========================================
    if deleted_moved_success:
        print(f"\n5. 🧹 正在通知大脑彻底清理 {len(deleted_moved_success)} 本书籍的 AI 数据和状态记录...")
        try:
            del_resp = requests.post(
                f"{MAC_MINI_API_URL}/api/novel/maintenance/confirm_delete",
                json={"filenames": deleted_moved_success},
                timeout=10
            )
            del_resp.raise_for_status()
            res_data = del_resp.json()
            print(f"✨ 数据库清理完成！大脑确认已抹除 {res_data.get('cleaned_count', 0)} 本书籍的相关记录。")
        except Exception as e:
            print(f"❌ 通知清理失败: {e}")
    else:
        print("✨ 没有需要清理的垃圾记录。")
        
    input("\n处理完毕，按回车键退出:")

if __name__ == '__main__':
    main()