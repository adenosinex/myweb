import os
import subprocess
import argparse
import requests
import time
import datetime
from pathlib import Path
from tqdm import tqdm

def get_beijing_time():
    """获取当前的北京时间时刻"""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))

def get_duration(filepath):
    """ffprobe 极速扫描元数据获取时长"""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', 
           '-of', 'default=noprint_wrappers=1:nokey=1', str(filepath)]
    try:
        return float(subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip())
    except:
        return 0.0

def run_task():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", help="视频路径或文件夹")
    parser.add_argument("--url", default="http://apple4.zin6.dpdns.org:8089/upload")
    args = parser.parse_args()

    root = Path(args.path).absolute()
    suffix = "_compressed"
    archive_name = "original video"
    
    # 1. 快速索引
    print(f"🔍 正在扫描: {root.name}")
    raw_files = []
    if root.is_file():
        raw_files = [root] if root.suffix.lower() in ('.mp4', '.mov') else []
    else:
        raw_files = [f for f in root.rglob('*') if f.suffix.lower() in ('.mp4', '.mov') 
                     and suffix not in f.stem and archive_name not in str(f)]

    tasks = []
    total_sec = 0.0
    for f in raw_files:
        d = get_duration(f)
        if d > 0:
            tasks.append({'path': f, 'dur': d})
            total_sec += d

    if not tasks:
        print("未发现有效视频。")
        return

    # 2. 全局状态
    state = {"start_ts": time.time(), "total_sec": total_sec, "done_sec": 0.0}
    print(f"📊 任务就绪 | 共 {len(tasks)} 个文件 | 总长 {int(total_sec)}s")

    # 3. 循环处理
    for idx, t in enumerate(tasks):
        print(f"\n[{idx+1}/{len(tasks)}] {t['path'].name}")
        process_one(t, args.url, suffix, archive_name, state)

def process_one(task, url, suffix, archive_name, state):
    in_p = task['path']
    out_p = in_p.parent / f"{in_p.stem}{suffix}{in_p.suffix}"
    marker = b"===FILE-START===\n"
    
    # 进度条格式优化：n/total 现在代表视频秒数
    fmt = "{desc}: {percentage:3.0f}%|{bar}| {n:.1f}/{total_fmt}s [{elapsed}<{remaining}, {rate_fmt} {postfix}]"
    
    try:
        with requests.post(url, files={'file': in_p.open('rb')}, stream=True) as r:
            r.raise_for_status()
            with tqdm(total=task['dur'], unit="s", bar_format=fmt, dynamic_ncols=True) as pbar:
                with out_p.open('wb') as f:
                    file_data_mode = False
                    curr_file_sec = 0.0
                    buffer = bytearray()
                    
                    for chunk in r.iter_content(chunk_size=128*1024):
                        if not file_data_mode:
                            buffer.extend(chunk)
                            m_idx = buffer.find(marker)
                            if m_idx != -1:
                                file_data_mode = True
                                # 最后的进度解析
                                curr_file_sec = parse_log(buffer[:m_idx].decode(errors='ignore'), pbar, curr_file_sec, state)
                                pbar.set_description("💾 下载")
                                pbar.update(max(0, task['dur'] - pbar.n))
                                f.write(buffer[m_idx+len(marker):])
                                buffer.clear()
                            else:
                                # 实时解析进度文本
                                lines = buffer.split(b'\n')
                                for line in lines[:-1]:
                                    curr_file_sec = parse_log(line.decode(errors='ignore'), pbar, curr_file_sec, state)
                                buffer = bytearray(lines[-1])
                        else:
                            f.write(chunk)
        
        # 归档
        arc_dir = in_p.parent / archive_name
        arc_dir.mkdir(exist_ok=True)
        os.renames(str(in_p), str(arc_dir / in_p.name))
        
    except Exception as e:
        print(f"❌ 错误: {e}")
        if out_p.exists(): out_p.unlink()

def parse_log(text, pbar, last_s, state):
    if "out_time_us=" in text:
        try:
            cur_s = int(text.split("=")[1]) / 1000000.0
            diff = cur_s - last_s
            if diff > 0:
                pbar.update(diff)
                state["done_sec"] += diff
                # 计算全盘时刻
                run_time = time.time() - state["start_ts"]
                if state["done_sec"] > 0:
                    speed = state["done_sec"] / run_time
                    eta_s = (state["total_sec"] - state["done_sec"]) / speed
                    eta_t = get_beijing_time() + datetime.timedelta(seconds=eta_s)
                    pbar.set_postfix_str(f"🚩 {eta_t.strftime('%H:%M')} 结束")
                return cur_s
        except: pass
    return last_s

if __name__ == "__main__":
    run_task()