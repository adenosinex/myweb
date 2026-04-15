import os
import argparse
import datetime
import subprocess
import requests
import statistics
from collections import Counter
from tqdm import tqdm

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.ts'}

def get_video_info(filepath):
    duration = 0.0
    width, height = 0, 0
    try:
        cmd_time = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', filepath]
        res_time = subprocess.run(cmd_time, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if res_time.stdout.strip(): duration = float(res_time.stdout.strip())

        cmd_res = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', filepath]
        res_res = subprocess.run(cmd_res, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if res_res.stdout.strip():
            w_str, h_str = res_res.stdout.strip().split('x')
            width, height = int(w_str), int(h_str)
    except Exception as e:
        pass
    return duration, width, height

def categorize_resolution(w, h):
    """画质分类器"""
    if w == 0 or h == 0: return "未知"
    if w >= h: # 横屏
        if h >= 2160 or w >= 3840: return "4K超清"
        if h >= 1080 or w >= 1920: return "1080P"
        if h >= 720 or w >= 1280: return "720P"
        return "标清及以下"
    else: # 竖屏
        if w >= 1080 or h >= 1920: return "竖屏高清"
        return "竖屏标清"

def scan_directory(target_dir, keyword):
    """两阶段扫描：聚合直方图分箱数据"""
    matched_files = []
    for root, _, files in os.walk(target_dir):
        for file in files:
            if os.path.splitext(file)[1].lower() in VIDEO_EXTENSIONS and keyword in file:
                matched_files.append(os.path.join(root, file))

    if not matched_files: return None
    tqdm.write(f"  -> [{keyword}] 命中 {len(matched_files)} 个文件，开始特征分箱...")

    # 数据收集容器
    sizes_mb = []
    durations_m = []
    resolutions = Counter()
    timeline = Counter()

    for filepath in tqdm(matched_files, desc=f"解析 {keyword[:10]}", unit="个", leave=False):
        # 大小与时间跨度
        stat = os.stat(filepath)
        size_mb = stat.st_size / (1024 * 1024)
        sizes_mb.append(size_mb)
        
        month_key = datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m')
        timeline[month_key] += 1
        
        # 时长与画质
        dur_s, w, h = get_video_info(filepath)
        durations_m.append(dur_s / 60)
        resolutions[categorize_resolution(w, h)] += 1

    # ====== 核心：数据分箱与特征推导 ======
    total_size_gb = sum(sizes_mb) / 1024
    total_duration_h = sum(durations_m) / 60
    
    # 特征推导逻辑
    median_size = statistics.median(sizes_mb) if sizes_mb else 0
    max_size = max(sizes_mb) if sizes_mb else 0
    if max_size > 3 * median_size and max_size > 1024:
        size_feature = "包含超大单文件"
    elif median_size < 150:
        size_feature = "普遍为小碎片文件"
    else:
        size_feature = "文件体积分布均匀"

    # 大小分段分布
    size_dist = {
        "<100MB": len([s for s in sizes_mb if s < 100]),
        "100MB-1GB": len([s for s in sizes_mb if 100 <= s < 1024]),
        ">1GB": len([s for s in sizes_mb if s >= 1024])
    }

    # 时长分段分布
    dur_dist = {
        "<3min": len([d for d in durations_m if d < 3]),
        "3-15min": len([d for d in durations_m if 3 <= d < 15]),
        ">15min": len([d for d in durations_m if d >= 15])
    }

    return {
        "summary": {
            "file_count": len(matched_files),
            "total_size_gb": round(total_size_gb, 2),
            "total_duration_h": round(total_duration_h, 2),
            "size_feature": size_feature
        },
        "charts": {
            "size_dist": size_dist,
            "duration_dist": dur_dist,
            "resolution_dist": dict(resolutions),
            "timeline": dict(sorted(timeline.items())) # 按年月排序
        }
    }

def run_sync_task(api_base, target_dir, force_rescan=False):
    url = f"{api_base}/card-tag/api/skip/search"
    print(f"[*] 开始连接服务器拉取任务: {api_base}")
    try:
        cards = requests.get(url).json()
    except Exception as e:
        print(f"[!] 无法连接到服务器: {e}")
        return

    print(f"[*] 成功拉取到 {len(cards)} 个认知卡片。")
    if force_rescan:
        print("[!] 注意: 强制重扫模式已开启，将覆盖所有已有校验数据。")

    success_count = 0
    skip_count = 0
    
    with tqdm(total=len(cards), desc="总体卡片进度", unit="卡") as pbar:
        for card in cards:
            query = card.get('query')
            if not query:
                pbar.update(1)
                continue
            
            # 【新增跳过逻辑】：如果不强制覆盖且已经有数据，则直接跳过
            if not force_rescan and card.get('stats_data'):
                tqdm.write(f"[~] 跳过已校验: [{query}]")
                skip_count += 1
                pbar.update(1)
                continue
                
            pbar.set_description(f"正在处理: {query[:12]}")
            stats = scan_directory(target_dir, query)
            
            if stats:
                try:
                    res = requests.post(
                        f"{api_base}/card-tag/api/skip/update_stats/{card.get('id')}", 
                        json=stats, timeout=10
                    )
                    if res.status_code == 200: 
                        success_count += 1
                        tqdm.write(f"[+] 云端写入成功: [{query}]")
                    else:
                        tqdm.write(f"[-] 推送失败 [{query}] 状态码: {res.status_code}")
                except Exception as e:
                    tqdm.write(f"[!] 网络异常 [{query}]: {e}")
            else:
                tqdm.write(f"[-] 忽略: [{query}] 本地未查找到关联文件。")
                
            pbar.update(1)
            
    print(f"\n[*] 任务结束！成功更新 {success_count} 个卡片，跳过 {skip_count} 个卡片。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本地视频扫描与卡片数据同步工具")
    parser.add_argument("-t", "--target", default=r'\\UGREEN-1E55\xin_Y\视频\upcloud-sex', help="要扫描的本地媒体目录绝对路径")
    parser.add_argument("-s", "--server", default='https://apple.zin6.dpdns.org:10443', help="服务器 API 基础地址")
    parser.add_argument("-f", "--force", action="store_true", help="强制重新扫描所有卡片，无视已校验状态")
    
    args = parser.parse_args()
    
    # 执行任务，传递 force 状态
    run_sync_task(args.server.rstrip('/'), args.target, args.force)