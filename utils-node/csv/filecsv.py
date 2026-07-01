import os
import csv
import hashlib
from pathlib import Path
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= 配置中心 =================
class Config:
    # 扫描与哈希配置 (针对网络驱动器优化)
    HEAD_SIZE = 128 * 1024       # 头部读取大小: 128KB
    SAMPLE_SIZE = 64 * 1024      # 采样点读取大小: 64KB
    MAX_WORKERS = 16             # 并发线程数 (网络IO可适当调高，如 16-32)
    
    # 输出配置
    CSV_FILENAME = "00_file_index_cache.csv"  # 统一在目标目录下生成此文件，加 00 方便置顶排在最前

# ================= 哈希处理模块 =================
class HashUtil:
    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def get_head_hash(path: Path) -> str:
        try:
            with open(path, "rb") as f:
                return HashUtil.hash_bytes(f.read(Config.HEAD_SIZE))
        except Exception:
            return ""

    @staticmethod
    def get_sample_hash(path: Path, file_size: int) -> str:
        try:
            with open(path, "rb") as f:
                if file_size < Config.SAMPLE_SIZE * 3:
                    return HashUtil.hash_bytes(f.read())
                
                # 取 3个采样点：10%, 50%, 90%
                points = [0.1, 0.5, 0.9]
                chunks = []
                for p in points:
                    f.seek(max(0, int(file_size * p)))
                    chunks.append(f.read(Config.SAMPLE_SIZE))
            return HashUtil.hash_bytes(b"".join(chunks))
        except Exception:
            return ""

# ================= 索引数据持久化模块 =================
class IndexManager:
    @staticmethod
    def load(csv_path: Path) -> dict:
        """加载已存在的 CSV 索引文件，以文件绝对路径为 Key"""
        index = {}
        if csv_path.exists():
            try:
                with open(csv_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        index[row['path']] = row
            except Exception as e:
                print(f"[警告] 读取历史索引失败，将重新扫描: {e}")
        return index

    @staticmethod
    def save(data: list, csv_path: Path):
        """将全量数据覆盖写入 CSV"""
        if not data:
            return
        keys = data[0].keys()
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(data)

# ================= 核心扫描模块 =================
class Scanner:
    @staticmethod
    def process_single_file(path: Path) -> dict:
        """单文件处理核心逻辑"""
        try:
            stat = path.stat()
            size = stat.st_size
            
            return {
                "path": str(path),
                "size": size,
                "mtime": int(stat.st_mtime),
                "head_hash": HashUtil.get_head_hash(path),
                "sample_hash": HashUtil.get_sample_hash(path, size)
            }
        except Exception as e:
            print(f"\n[错误] 无法处理文件 {path}: {e}")
            return None

    @staticmethod
    def run(folder: Path, existing_index: dict) -> list:
        """多线程扫描目录并跳过已有文件"""
        # 1. 收集所有文件路径（忽略 CSV 自身）
        all_paths = []
        for root, _, files in os.walk(folder):
            for name in files:
                if name == Config.CSV_FILENAME:
                    continue  # 跳过索引文件本身
                path = Path(root) / name
                all_paths.append(str(path))
        
        # 2. 筛选出需要新处理的文件
        new_files = [Path(p) for p in all_paths if p not in existing_index]
        print(f"-> 目录内共有文件: {len(all_paths)} 个")
        print(f"-> 历史已记录跳过: {len(all_paths) - len(new_files)} 个")
        print(f"-> 当前需处理新文件: {len(new_files)} 个")

        new_results = []
        if not new_files:
            return new_results

        # 3. 多线程并发处理新文件
        with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
            futures = {executor.submit(Scanner.process_single_file, path): path for path in new_files}
            
            for future in tqdm(as_completed(futures), total=len(new_files), desc="Scanning"):
                result = future.result()
                if result:
                    new_results.append(result)
                
        return new_results

# ================= 启动主入口 =================
def main():
    folder_input = input("输入目标扫描目录: ").strip()
    target_folder = Path(folder_input).resolve()
    
    if not target_folder.exists() or not target_folder.is_dir():
        print("[错误] 目录不存在或不是有效文件夹")
        return

    # 将 CSV 直接保存在扫描目标文件夹内部
    output_csv = target_folder / Config.CSV_FILENAME
    
    print("\n--- 1. 检查并加载历史索引 ---")
    existing_index = IndexManager.load(output_csv)
    
    print("\n--- 2. 开始扫描目录 ---")
    new_data = Scanner.run(target_folder, existing_index)
    
    print("\n--- 3. 数据合并与写入 ---")
    # 合并: 旧数据字典中的 values + 新扫描的列表数据
    final_data = list(existing_index.values()) + new_data
    IndexManager.save(final_data, output_csv)
    
    print(f"\n[处理完成] 总记录数: {len(final_data)}")
    print(f"[文件路径] 索引已保存至: {output_csv.absolute()}")

if __name__ == "__main__":
    main()