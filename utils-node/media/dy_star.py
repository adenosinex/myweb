import os
import shutil
import sqlite3
import hashlib
import datetime
import json
from pathlib import Path
from tqdm import tqdm

def get_partial_hash(filepath, chunk_size=1024 * 1024):
    """
    计算文件头部的部分哈希值（默认读取前 1MB），提升大文件比对速度。
    """
    hasher = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            chunk = f.read(chunk_size)
            hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def process_blacklist(db_path, blacklist_dir, size_tolerance=10240):
    """
    1. 扫描黑名单文件夹进行大小与头部 Hash 增量比对。
    2. 仅针对未打分视频 (score = 0 或 NULL) 进行处理，不影响已打分文件。
    3. 库内自去重：如果库内存在相同的未打分文件，只保留一份，其余打 1 星。
    """
    if not all([db_path, blacklist_dir]):
        print("缺少必要参数：需同时指定数据库路径(db_path)和黑名单文件夹(blacklist_dir)。")
        return

    db_path = Path(db_path)
    blacklist_dir = Path(blacklist_dir)

    if not db_path.exists():
        print(f"数据库文件不存在: {db_path}")
        return
    if not blacklist_dir.exists():
        print(f"黑名单文件夹不存在: {blacklist_dir}")
        return

    # 初始化或加载缓存
    cache_file = db_path.parent / "blacklist_cache.json"
    cache = {}
    cache_updated = False
    if cache_file.exists():
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            print("缓存文件读取失败，将重新建立缓存。")

    print(f"正在扫描黑名单文件夹: {blacklist_dir} ...")
    bl_files = []
    current_bl_paths = set()
    valid_exts = {'.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.wmv', '.m4v'}
    
    # 1. 扫描外部黑名单文件并校验缓存
    for root, _, files in os.walk(blacklist_dir):
        for file in files:
            if os.path.splitext(file)[1].lower() in valid_exts:
                full_path = Path(root) / file
                path_str = str(full_path)
                current_bl_paths.add(path_str)
                
                try:
                    stat = full_path.stat()
                    size = stat.st_size
                    mtime = stat.st_mtime
                    
                    cached_data = cache.get(path_str, {})
                    file_hash = None
                    
                    # 比对大小和修改时间判断是否复用 Hash
                    if cached_data.get('size') == size and cached_data.get('mtime') == mtime:
                        file_hash = cached_data.get('hash')
                    else:
                        cache[path_str] = {'size': size, 'mtime': mtime, 'hash': None}
                        cache_updated = True

                    bl_files.append({'path': full_path, 'path_str': path_str, 'size': size, 'hash': file_hash})
                except OSError:
                    continue

    if not bl_files:
        print("警告：黑名单文件夹中未找到支持的视频文件，将仅执行库内自去重。")

    # 清理已失效的黑名单缓存记录
    keys_to_remove = [k for k in cache.keys() if k not in current_bl_paths]
    if keys_to_remove:
        for k in keys_to_remove:
            del cache[k]
        cache_updated = True

    print(f"发现外部黑名单样本 {len(bl_files)} 个，开始黑名单比对与库内自去重...")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        # 核心优化：只取未处理(0或NULL)的记录，绝对不碰已打分文件
        cursor.execute("SELECT id, detail, file_size FROM videos WHERE score = 0 OR score IS NULL")
        db_records = cursor.fetchall()
        
        matched_ids = []
        kept_db_files = []  # 用于记录目前已经保留的库内未打分文件，结构: dict

        with tqdm(total=len(db_records), desc="智能比对/去重进度") as pbar:
            for db_id, db_path_str, db_size in db_records:
                if not db_path_str or db_size is None:
                    pbar.update(1)
                    continue

                db_file_path = Path(db_path_str)
                db_hash = None  
                is_duplicate = False

                # 步骤一：比对外部黑名单
                for bl in bl_files:
                    if abs(db_size - bl['size']) <= size_tolerance:
                        if db_hash is None:
                            db_hash = get_partial_hash(db_file_path)
                            if not db_hash:
                                break  # 读文件失败跳过
                        
                        if bl['hash'] is None:
                            bl['hash'] = get_partial_hash(bl['path'])
                            if bl['hash']:
                                cache[bl['path_str']]['hash'] = bl['hash']
                                cache_updated = True
                        
                        if bl['hash'] and db_hash == bl['hash']:
                            # 【改动点】：增加一个 1 (代表 cp=1)，使其自动标记为已处理
                            matched_ids.append((1, 1, datetime.datetime.now(), db_id))
                            is_duplicate = True
                            break  

                # 步骤二：如果没有命中黑名单，进行库内自去重
                if not is_duplicate:
                    for kept in kept_db_files:
                        if abs(db_size - kept['size']) <= size_tolerance:
                            if db_hash is None:
                                db_hash = get_partial_hash(db_file_path)
                                if not db_hash:
                                    break
                            
                            # 惰性计算库内已保留文件的 Hash
                            if kept['hash'] is None:
                                kept['hash'] = get_partial_hash(kept['path'])
                            
                            if kept['hash'] and db_hash == kept['hash']:
                                # 【改动点】：增加一个 1 (代表 cp=1)，使其自动标记为已处理
                                matched_ids.append((1, 1, datetime.datetime.now(), db_id))
                                is_duplicate = True
                                break

                # 步骤三：既非黑名单，也非库内重复，正式作为保留基准记录下来
                if not is_duplicate:
                    kept_db_files.append({
                        'size': db_size,
                        'path': db_file_path,
                        'hash': db_hash  # 可能是 None，用到时才会计算
                    })
                
                pbar.update(1)

        # 批量更新数据库状态
        if matched_ids:
            # 【改动点】：SQL语句增加 cp = ? 的更新，同步将自动打回的视频标记为跳过复制
            cursor.executemany("UPDATE videos SET score = ?, cp = ?, updatetime = ? WHERE id = ?", matched_ids)
            conn.commit()
            print(f"\n处理完成：共自动清理(打1星) {len(matched_ids)} 个视频 (含外部黑名单与库内重复项)。")
        else:
            print("\n处理完成：所有未打分文件均无重复或命中黑名单。")

    except sqlite3.Error as e:
        conn.rollback()
        print(f"数据库操作异常: {e}")
    except Exception as e:
        print(f"操作发生异常: {e}")
    finally:
        conn.close()
        
    # 固化缓存
    if cache_updated:
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
            print(f"黑名单缓存已同步至: {cache_file}")
        except Exception as e:
            print(f"保存缓存文件失败: {e}") 


def _copy_by_score(db_path, dst_dir, src_base_dir, target_score):
    """
    通用底层函数：直接使用原生 sqlite3 读取数据库。
    将指定分数的记录，保持相对路径复制/硬链接到 dst_dir。
    遇到同名文件时，通过大小和哈希比对，不同则重命名复制。
    """
    if not all([db_path, dst_dir, src_base_dir]):
        print("缺少必要参数：需同时指定数据库路径(db_path)、目标文件夹(dst_dir)和源基础文件夹(src_base_dir)。")
        return

    db_path = Path(db_path)
    dst_dir = Path(dst_dir)
    src_base_dir = Path(src_base_dir)

    if not db_path.exists():
        print(f"数据库文件不存在: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # 仅查询未被标记为已复制 (cp = 0 或 NULL) 的文件
        cursor.execute("SELECT detail FROM videos WHERE score = ? AND (cp = 0 OR cp IS NULL) ORDER BY updatetime", (target_score,))
        rows = cursor.fetchall()
        
        if not rows:
            print(f"[*] 数据库中没有发现需要新处理的 {target_score} 星视频。")
            return

        tasks = []
        for row in rows:
            src_path_str = row[0]
            if not src_path_str:
                continue
            
            src_path = Path(src_path_str)
            if not src_path.exists():
                print(f"[跳过] 源文件在硬盘上已不存在: {src_path}")
                continue
                
            try:
                rel_path = src_path.relative_to(src_base_dir)
            except ValueError:
                print(f"[跳过] 路径不匹配，无法计算相对路径。\n  -> 源文件: {src_path}\n  -> 基础目录: {src_base_dir}")
                continue 

            dst_path = dst_dir.joinpath(rel_path)
            tasks.append((src_path, dst_path, src_path_str))

        if not tasks:
            print(f"[*] 解析后有效的 {target_score} 星文件路径数量为 0。请检查上方是否有 [跳过] 提示。")
            return

        cnt = 0
        success_records = []
        
        with tqdm(total=len(tasks), desc=f"复制 {target_score} 星进度") as pbar:
            for src_path, dst_path, original_detail_str in tasks:
                # ===== 冲突检测与 Hash 校验逻辑 =====
                if dst_path.exists():
                    try:
                        src_size = src_path.stat().st_size
                        dst_size = dst_path.stat().st_size
                        
                        # 1. 先对比大小，大小一致再计算 Hash (复用脚本头部的 get_partial_hash 函数)
                        if src_size == dst_size:
                            src_hash = get_partial_hash(src_path)
                            dst_hash = get_partial_hash(dst_path)
                            
                            # 2. Hash 一致，确认为同一文件，直接跳过并标记为已复制
                            if src_hash and src_hash == dst_hash:
                                success_records.append((1, original_detail_str))
                                pbar.update(1)
                                continue
                    except Exception as e:
                        pass # 遇到权限等不可读异常，默认按冲突处理

                    # 3. 大小或 Hash 不同，判定为同名不同内容文件，重命名目标路径以免覆盖
                    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                    dst_path = dst_path.with_name(f"{dst_path.stem}_{timestamp}{dst_path.suffix}")
                    print(f"\n[重命名] 目标已存在同名不同内容文件，新路径: {dst_path.name}")
                # ====================================

                dst_path.parent.mkdir(parents=True, exist_ok=True)
                
                success = False
                try:
                    os.link(src_path, dst_path)
                    success = True
                except OSError:
                    try:
                        shutil.copy2(src_path, dst_path)
                        success = True
                    except Exception as e:
                        print(f"\n[失败] 复制异常 [{src_path}]: {e}")
                
                if success:
                    success_records.append((1, original_detail_str))
                    cnt += 1
                
                pbar.update(1)

        if success_records:
            cursor.executemany("UPDATE videos SET cp = ? WHERE detail = ?", success_records)
            conn.commit()
            
        print(f"[*] 共成功复制分数为 {target_score} 的视频文件：{cnt} 个")

    except sqlite3.Error as e:
        conn.rollback()
        print(f"数据库操作异常: {e}")
    except Exception as e:
        print(f"操作发生异常: {e}")
    finally:
        conn.close()

 
def copy_5score(db_path, dst_dir, src_base_dir):
    """复制分数为 5 的记录"""
    _copy_by_score(db_path, dst_dir, src_base_dir, 5)

def copy_1score(db_path, dst_dir, src_base_dir):
    """复制分数为 1 的记录"""
    _copy_by_score(db_path, dst_dir, src_base_dir, 1)


if __name__ == "__main__":
    while True:
        print("\n--- 视频数据库辅助工具 ---")
        print("1. 复制 5 星视频")
        print("2. 复制 1 星视频")
        print("3. 执行黑名单扫描匹配")
        print("0. 退出")
        choice = input("请输入对应操作编号: ").strip()
        
        db_path_val = r"C:\Users\xin-a\Videos\core_app\db\videos.db"
        
        if choice == '1':
            copy_5score(
                db_path=db_path_val,
                dst_dir=r"\\One\d\move video\heart\new",
                src_base_dir=r"\\One\d\downloadD"
            )
        elif choice == '2':
            copy_1score(
                db_path=db_path_val,
                dst_dir=r"\\One\d\move video\trash",
                src_base_dir=r"\\One\d\downloadD"
            )
        elif choice == '3':
            bl_dir = r'\\One\d\move video\bad'
            if bl_dir:
                process_blacklist(
                    db_path=db_path_val,
                    blacklist_dir=bl_dir,
                    size_tolerance=10240
                )
        elif choice == '0':
            break
        else:
            print("输入无效。")
            
       