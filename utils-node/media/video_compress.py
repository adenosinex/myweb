import os
import subprocess
import argparse
import requests
import json
import shutil
import traceback
from tqdm import tqdm

def get_video_info(filepath):
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=height,bit_rate:format=duration,bit_rate',
        '-of', 'json', filepath
    ]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
        data = json.loads(output)
        
        streams = data.get('streams', [])
        stream = streams[0] if streams else {}
        fmt = data.get('format', {})
        
        duration = float(fmt.get('duration', 0.0))
        height = int(stream.get('height', 0))
        
        bit_rate = stream.get('bit_rate') or fmt.get('bit_rate') or 0
        bitrate_kbps = float(bit_rate) / 1000
        
        return duration, bitrate_kbps, height
    except Exception as e:
        print(f"⚠️ 无法解析视频元数据 {os.path.basename(filepath)}: {e}")
        return 0.0, float('inf'), float('inf')

def process_path(target_path, server_url, target_res, skip_bitrate_kbps, crf, preset, out_dir=None):
    target_path = os.path.abspath(target_path)
    if not os.path.exists(target_path):
        print(f"❌ 无效的输入路径: {target_path}")
        return

    suffix = "_compressed"
    files = []

    if os.path.isfile(target_path):
        if target_path.lower().endswith(('.mp4', '.mov', '.m4v')):
            files = [target_path]
        else:
            print(f"不支持的文件格式: {target_path}")
            return
            
    elif os.path.isdir(target_path):
        print(f"📁 检测到目录，开始递归深度扫描: {target_path}")
        for root, dirs, filenames in os.walk(target_path):
            for f in filenames:
                if f.lower().endswith(('.mp4', '.mov', '.m4v')):
                    files.append(os.path.join(root, f))
        
        if not files:
            print("目录及其子目录中没有找到支持的视频文件。")
            return
    
    files.sort(key=lambda x: os.path.getsize(x))
            
    for idx, vf in enumerate(files):
        try:
            filename = os.path.basename(vf)
            name, ext = os.path.splitext(filename)
            file_dir = os.path.dirname(vf)
            
            if out_dir:
                abs_out_dir = os.path.abspath(out_dir)
                os.makedirs(abs_out_dir, exist_ok=True)
                output_path = os.path.join(abs_out_dir, f"{name}{suffix}{ext}")
            else:
                if os.path.basename(file_dir) == 'original video':
                    output_path = os.path.join(os.path.dirname(file_dir), f"{name}{suffix}{ext}")
                else:
                    output_path = os.path.join(file_dir, f"{name}{suffix}{ext}")

            if os.path.exists(output_path):
                print(f"⏭️ [跳过] 目标输出文件已存在: {output_path}")
                if os.path.basename(file_dir) != 'original video':
                    dst = os.path.join(file_dir, 'original video')
                    os.makedirs(dst, exist_ok=True)
                    try:
                        shutil.move(vf, os.path.join(dst, filename))
                    except Exception:
                        pass
                continue
            
            if name.endswith(suffix):
                continue

            duration, bitrate_kbps, height = get_video_info(vf)
            if height <= target_res and 0 < bitrate_kbps <= skip_bitrate_kbps:
                print(f"⏭️ [跳过] 画质/码率已极低，无需压缩 | 高度: {height}p, 码率: {bitrate_kbps:.0f} kbps - {filename}")
                if os.path.basename(file_dir) != 'original video':
                    dst = os.path.join(file_dir, 'original video')
                    os.makedirs(dst, exist_ok=True)
                    try:
                        shutil.move(vf, os.path.join(dst, filename))
                    except Exception:
                        pass
                continue

            dst_archive = file_dir if os.path.basename(file_dir) == 'original video' else os.path.join(file_dir, 'original video')
            
            print(f"\n--- 正在处理第 {idx+1}/{len(files)} 个文件 (大小: {os.path.getsize(vf)/1024/1024:.1f}MB) ---")
            compress_stream(vf, server_url, dst_archive, duration, target_res, crf, preset, output_path)
            
        except Exception:
            print(f"\n❌ 处理文件 {os.path.basename(vf)} 时发生异常:")
            print(traceback.format_exc())
            print("⏭️ 自动跳过该文件，继续处理队列中的下一个视频...\n")

def compress_stream(input_path, server_url, dst_archive, duration, target_res, crf, preset, output_path):
    filename = os.path.basename(input_path)
    print(f"正在高速传输并请求压缩: {filename}")
    
    marker = b"===FILE-START===\n"
    buffer = bytearray()
    file_started = False
    last_progress = 0.0

    # 核心优化 1：将参数转移到 URL Query 中
    params = {
        'filename': filename,
        'target_resolution': target_res,
        'crf': crf,
        'preset': preset
    }

    try:
        # 核心优化 2：直接灌入文件句柄，绕过表单编码，打满千兆网卡
        with open(input_path, 'rb') as f_in:
            with requests.post(server_url, params=params, data=f_in, stream=True, timeout=86400) as response:
                response.raise_for_status()
                
                with tqdm(total=100, desc="云端压缩", unit="%") as pbar:
                    with open(output_path, 'wb') as fout:
                        # 核心优化 3：将读取块大小从 8KB 暴增到 1MB，大幅提升回传性能
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not file_started:
                                buffer.extend(chunk)
                                idx = buffer.find(marker)
                                
                                if idx != -1:
                                    file_started = True
                                    text_data = buffer[:idx].decode('utf-8', errors='ignore')
                                    _update_pbar_from_text(text_data, duration, pbar, last_progress)
                                    
                                    if pbar.n < 100:
                                        pbar.update(100 - pbar.n)
                                    pbar.set_description("接收回传文件")
                                    
                                    fout.write(buffer[idx + len(marker):])
                                    buffer.clear()
                                else:
                                    lines = buffer.split(b'\n')
                                    for line in lines[:-1]:
                                        text = line.decode('utf-8', errors='ignore')
                                        last_progress = _update_pbar_from_text(text, duration, pbar, last_progress)
                                    buffer = bytearray(lines[-1])
                            else:
                                fout.write(chunk)
                                
    except requests.exceptions.RequestException as e:
        print(f"\n❌ 网络请求失败或服务端中断: {e}")
        if getattr(e, 'response', None) is not None:
            print(f"服务端返回状态码: {e.response.status_code}")
            print(f"服务端返回内容: {e.response.text[:500]}")
            
        if os.path.exists(output_path):
            os.remove(output_path)
        raise e
        
    if file_started:
        print(f"\n✅ 成功接收并保存: {output_path}")
        os.makedirs(dst_archive, exist_ok=True)
        target_original_path = os.path.join(dst_archive, filename)
        
        if os.path.abspath(input_path) != os.path.abspath(target_original_path):
            try:
                shutil.move(input_path, target_original_path)
                print(f"📦 原文件已归档至: {target_original_path}")
            except Exception as e:
                print(f"⚠️ 归档原文件失败: {e}")
        else:
            print(f"📦 原文件已存在于归档夹内。")
    else:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError("服务端未能成功返回视频流数据（可能转码器报错）。")

def _update_pbar_from_text(text, duration, pbar, last_progress):
    if "out_time_us=" in text and duration > 0:
        try:
            out_time_us = int(text.split("=")[1])
            current_sec = out_time_us / 1_000_000
            prog = min(100.0, (current_sec / duration) * 100)
            if prog > last_progress:
                pbar.update(prog - last_progress)
                return prog
        except Exception:
            pass
    return last_progress

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="流式视频压缩客户端")
    parser.add_argument("filepath", help="要压缩的视频路径或目录")
    parser.add_argument("--url", default="http://apple4.su7.dpdns.org:8005/upload", help="服务端完整 /upload 接口地址")
    parser.add_argument("--res", type=int, default=1080, help="目标高度(P)，默认 1080")
    parser.add_argument("--skip-bitrate", type=float, default=3000, help="跳过阈值(kbps)，原视频小于此码率时直接归档不压缩，默认 3000")
    parser.add_argument("--crf", type=int, default=22, help="H.265 CRF 质量值，默认 22")
    parser.add_argument("--preset", type=str, default="medium", choices=['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'], help="x265 预设编码速度，默认 medium")
    parser.add_argument("--out-dir", type=str, default=r'C:\Users\xin\Videos\videocompres', help="指定压缩后视频的输出目录（可选）")
    
    args = parser.parse_args()
    process_path(args.filepath, args.url, args.res, args.skip_bitrate, args.crf, args.preset, args.out_dir)