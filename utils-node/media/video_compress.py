import os
import subprocess
import requests
import json
import shutil
import traceback
import time
import tempfile
from tqdm import tqdm

try:
    import cv2
except ImportError:
    cv2 = None
    print("⚠️ 缺少 opencv-python 库，智能分析模式将受限。如需使用智能模式请执行: pip install opencv-python")

# 1. 集中化参数配置类
class CompressConfig:
    def __init__(self):
        # 输入路径（文件或目录）
        self.filepath = './'
        # 服务端完整 /upload 接口地址
        self.server_url = "http://apple4.su7.dpdns.org:8005/upload"
        # 目标高度(P)
        self.target_res = 1080
        # 跳过阈值(kbps)
        self.skip_bitrate_kbps = 3000
        # H.265 CRF 基础质量值
        self.crf = 28
        # x265 预设编码速度
        self.preset = "medium"
        
        # --- 目录与路径控制 (动态子目录模式) ---
        # 压缩后视频的输出目录名称（会自动在 filepath 同级或内部生成）
        self.out_dir_name = "videocompres"
        # 原视频归档主目录名称（会自动在 filepath 同级或内部生成）
        self.archive_dir_name = "origin"
        # 【核心开关】是否在输出和归档时，保留原视频相对 filepath 的多级目录结构
        self.preserve_relative_path = True

# 全局统计数据
class TaskStats:
    total_files = 0
    compressed_files = 0
    skipped_files = 0
    total_original_size = 0
    total_compressed_size = 0
    total_process_time = 0.0

    @classmethod
    def reset(cls):
        cls.total_files = 0
        cls.compressed_files = 0
        cls.skipped_files = 0
        cls.total_original_size = 0
        cls.total_compressed_size = 0
        cls.total_process_time = 0.0

def get_video_info(filepath):
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=height,bit_rate,codec_name,r_frame_rate:format=duration,bit_rate,size',
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
        size = int(fmt.get('size', 0))
        codec = stream.get('codec_name', '')
        
        bit_rate = stream.get('bit_rate') or fmt.get('bit_rate') or 0
        bitrate_kbps = float(bit_rate) / 1000
        
        fps = 30.0
        r_frame_rate = stream.get('r_frame_rate', '30/1')
        if '/' in r_frame_rate:
            num, den = r_frame_rate.split('/')
            if int(den) > 0:
                fps = float(num) / float(den)
        
        return duration, bitrate_kbps, height, codec, fps, size
    except Exception as e:
        print(f"⚠️ 无法解析视频元数据 {os.path.basename(filepath)}: {e}")
        return 0.0, float('inf'), float('inf'), '', 30.0, 0

def smart_analyze_video(filepath, duration, original_size, codec, fps, config: CompressConfig):
    """
    智能分析视频收益、运动量和复杂度
    返回: (should_skip, skip_reason, adjusted_crf, target_fps)
    """
    predicted_ratio = 1.0
    if codec in ['hevc', 'h265']:
        predicted_ratio = 0.92
    elif codec in ['h264']:
        predicted_ratio = 0.65
    else:
        predicted_ratio = 0.45

    if predicted_ratio > 0.9:
        return True, "预计压缩收益不足10% (基于源编码推算)", config.crf, fps

    target_fps = 30.0 if fps >= 50.0 else fps
    adjusted_crf = config.crf
    
    if duration > 10 and cv2 is not None:
        try:
            cap = cv2.VideoCapture(filepath)
            timestamps = [duration * i / 5 for i in range(1, 5)]
            motion_scores = []
            complexity_scores = []
            
            for t in timestamps:
                cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
                ret1, frame1 = cap.read()
                ret2, frame2 = cap.read() 
                
                if ret1 and ret2:
                    gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
                    gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
                    
                    motion = cv2.absdiff(gray1, gray2).mean()
                    motion_scores.append(motion)
                    
                    edges = cv2.Canny(gray1, 50, 150).mean()
                    complexity_scores.append(edges)
            cap.release()
            
            avg_motion = sum(motion_scores) / len(motion_scores) if motion_scores else 10.0
            avg_complexity = sum(complexity_scores) / len(complexity_scores) if complexity_scores else 20.0
            
            if avg_motion < 3.0:
                print("  [分析] 画面运动量极低 (如PPT/监控等)")
                target_fps = 30.0
            else:
                target_fps = fps if fps < 60 else 60.0
                
            if avg_complexity < 5.0:
                print("  [分析] 场景复杂度低 (如动漫/纯色)")
                adjusted_crf = config.crf + 2
            elif avg_complexity > 30.0:
                print("  [分析] 场景复杂度高 (如树叶/雪景)")
                adjusted_crf = config.crf - 2

        except Exception as e:
            print(f"  [分析] OpenCV抽样失败，使用默认参数 ({e})")
            
    if original_size > 500 * 1024 * 1024 and duration >= 10:
        print("  [分析] 触发大文件 5s 极速试压检测...")
        mid_time = duration / 2
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            temp_out = f.name
            
        sample_cmd = [
            'ffmpeg', '-y', '-v', 'error',
            '-ss', str(mid_time), '-t', '5',
            '-i', filepath,
            '-c:v', 'libx265', '-crf', str(adjusted_crf), '-preset', config.preset,
            '-an', temp_out
        ]
        
        try:
            subprocess.run(sample_cmd, timeout=30)
            if os.path.exists(temp_out):
                sample_size = os.path.getsize(temp_out)
                os.remove(temp_out)
                
                orig_5s_size = original_size * (5.0 / duration)
                sample_ratio = sample_size / orig_5s_size if orig_5s_size > 0 else 1.0
                
                print(f"  [试压] 样本测试比率: {sample_ratio:.2f}")
                if sample_ratio > 0.9:
                    return True, "样本试压测试显示收益不足10%", adjusted_crf, target_fps
        except Exception:
            print("  [试压] 试压超时或失败，跳过试压判断")
            if os.path.exists(temp_out):
                os.remove(temp_out)

    return False, "", adjusted_crf, target_fps

def compress_stream(input_path, output_path, dst_archive, duration, target_crf, target_fps, config: CompressConfig):
    filename = os.path.basename(input_path)
    print(f"🚀 正在高速传输并请求压缩...")
    
    marker = b"===FILE-START===\n"
    buffer = bytearray()
    file_started = False
    last_progress = 0.0

    params = {
        'filename': filename,
        'target_resolution': config.target_res,
        'crf': target_crf,
        'preset': config.preset,
        'fps': target_fps
    }

    try:
        with open(input_path, 'rb') as f_in:
            with requests.post(config.server_url, params=params, data=f_in, stream=True, timeout=86400) as response:
                response.raise_for_status()
                
                with tqdm(total=100, desc="云端压缩", unit="%") as pbar:
                    with open(output_path, 'wb') as fout:
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
        if os.path.exists(output_path):
            os.remove(output_path)
        raise e
        
    if file_started:
        print(f"\n✅ 成功接收并保存: {output_path}")
        os.makedirs(dst_archive, exist_ok=True)
        target_original_path = os.path.join(dst_archive, filename)
        
        if os.path.abspath(input_path) != os.path.abspath(target_original_path):
            try: shutil.move(input_path, target_original_path)
            except Exception: pass
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

def _core_process_pipeline(config: CompressConfig, use_smart_mode: bool):
    target_path = os.path.abspath(config.filepath)
    if not os.path.exists(target_path):
        print(f"❌ 无效的输入路径: {target_path}")
        return

    # 获取基准目录（单文件为其所在目录，目录则为目录本身）
    base_input_dir = target_path if os.path.isdir(target_path) else os.path.dirname(target_path)
    
    # 根据基准目录，动态生成输出和归档的主绝对路径
    base_out_dir = os.path.join(base_input_dir, config.out_dir_name) if config.out_dir_name else base_input_dir
    base_archive_dir = os.path.join(base_input_dir, config.archive_dir_name) if config.archive_dir_name else base_input_dir

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
            # 自动跳过动态生成的输出或归档目录，防止死循环无限处理
            if base_out_dir in os.path.abspath(root) or base_archive_dir in os.path.abspath(root):
                continue
                
            for f in filenames:
                if f.lower().endswith(('.mp4', '.mov', '.m4v')):
                    files.append(os.path.join(root, f))
        
        if not files:
            print("目录及其有效子目录中没有找到支持的视频文件。")
            return
    
    files.sort(key=lambda x: os.path.getsize(x))
    TaskStats.reset()
    TaskStats.total_files = len(files)
    global_start_time = time.time()
            
    for idx, vf in enumerate(files):
        try:
            filename = os.path.basename(vf)
            name, ext = os.path.splitext(filename)
            file_dir = os.path.dirname(vf)
            original_size = os.path.getsize(vf)
            
            # 计算当前文件相对于基准目录的相对路径
            if config.preserve_relative_path:
                rel_path = os.path.relpath(file_dir, base_input_dir)
                if rel_path == '.':
                    rel_path = ''
            else:
                rel_path = ''
                
            # 动态拼接输出路径
            current_out_dir = os.path.join(base_out_dir, rel_path) if rel_path else base_out_dir
            os.makedirs(current_out_dir, exist_ok=True)
            output_path = os.path.join(current_out_dir, f"{name}{suffix}{ext}")

            # 动态拼接归档路径
            skip_dir_base = os.path.join(base_archive_dir, "skip")
            success_dir_base = base_archive_dir
            
            skip_archive = os.path.join(skip_dir_base, rel_path) if rel_path else skip_dir_base
            dst_archive = os.path.join(success_dir_base, rel_path) if rel_path else success_dir_base

            def archive_skipped_file():
                os.makedirs(skip_archive, exist_ok=True)
                target_file = os.path.join(skip_archive, filename)
                if os.path.abspath(vf) != os.path.abspath(target_file):
                    pass
                    # try: shutil.move(vf, target_file)
                    # except: pass

            print(f"\n--- 正在处理第 {idx+1}/{len(files)} 个文件 (大小: {original_size/1024/1024:.1f}MB) ---")
            print(f"📁 {filename}")

            if name.endswith(suffix):
                print(f"⏭️ [跳过] 规则4: 文件已为压缩输出文件")
                TaskStats.skipped_files += 1
                continue
            
            if os.path.exists(output_path):
                print(f"⏭️ [跳过] 规则5: 目标输出文件已存在")
                TaskStats.skipped_files += 1
                archive_skipped_file()
                continue

            duration, bitrate_kbps, height, codec, fps, size = get_video_info(vf)
            
            if height <= config.target_res and 0 < bitrate_kbps <= config.skip_bitrate_kbps:
                print(f"⏭️ [跳过] 规则1: 分辨率/码率已极低 | 码率: {bitrate_kbps:.0f} kbps")
                TaskStats.skipped_files += 1
                archive_skipped_file()
                continue

            if use_smart_mode:
                t_analyze_start = time.time()
                should_skip, skip_reason, crf_final, fps_final = smart_analyze_video(
                    vf, duration, size, codec, fps, config
                )
                t_analyze = time.time() - t_analyze_start
                print(f"💡 分析耗时: {t_analyze:.1f}s | 原码率: {bitrate_kbps:.0f}kbps | 编码: {codec} | 推荐CRF: {crf_final} | 目标帧率: {fps_final:.0f}fps")
                
                if should_skip:
                    print(f"⏭️ [跳过] {skip_reason}")
                    TaskStats.skipped_files += 1
                    archive_skipped_file()
                    continue
            else:
                crf_final = config.crf
                fps_final = fps
                print(f"⚙️ 机械模式: 固定使用 CRF {crf_final} | 目标帧率保持 {fps_final:.0f}fps")
            
            t_compress_start = time.time()
            compress_stream(vf, output_path, dst_archive, duration, crf_final, fps_final, config)
            t_compress = time.time() - t_compress_start

            if os.path.exists(output_path):
                compressed_size = os.path.getsize(output_path)
                TaskStats.compressed_files += 1
                TaskStats.total_original_size += original_size
                TaskStats.total_compressed_size += compressed_size
                TaskStats.total_process_time += t_compress
                
                ratio = compressed_size / original_size if original_size > 0 else 1.0
                print(f"📊 [日志统计] 原大小: {original_size/1024/1024:.1f}MB -> 实际大小: {compressed_size/1024/1024:.1f}MB | 压缩率: {ratio*100:.1f}% | 耗时: {t_compress:.1f}s")
            
        except Exception:
            print(f"\n❌ 处理文件 {os.path.basename(vf)} 时发生异常:")
            print(traceback.format_exc())
            print("⏭️ 自动跳过该文件，继续处理队列中的下一个视频...\n")

    print("\n" + "="*40)
    mode_name = "智能分析压缩" if use_smart_mode else "机械化指定画质压缩"
    print(f"📈 批量处理任务结束汇总 ({mode_name})")
    print("="*40)
    print(f"总文件数: {TaskStats.total_files}")
    print(f"压缩文件数: {TaskStats.compressed_files}")
    print(f"跳过文件数: {TaskStats.skipped_files}")
    
    if TaskStats.compressed_files > 0:
        saved_space = TaskStats.total_original_size - TaskStats.total_compressed_size
        avg_ratio = TaskStats.total_compressed_size / TaskStats.total_original_size
        print(f"节省空间总量: {saved_space/1024/1024:.1f} MB")
        print(f"平均压缩率: {avg_ratio*100:.1f} %")
    else:
        print("节省空间总量: 0 MB")
        print("平均压缩率: N/A")
        
    print(f"总处理耗时: {time.time() - global_start_time:.1f} 秒")
    print("="*40)

def run_smart_compression(config: CompressConfig):
    print(f"🚀 启动【智能模式】压缩任务...")
    _core_process_pipeline(config, use_smart_mode=True)

def run_fixed_compression(config: CompressConfig):
    print(f"🚀 启动【机械模式】压缩任务...")
    _core_process_pipeline(config, use_smart_mode=False)


if __name__ == "__main__":
    # 实例化配置
    cfg = CompressConfig()
    
    # 指定需要处理的绝对路径（文件或目录均可）
    cfg.filepath = input('cmp:')
    
    # 改为目录名称变量，程序会自动在 filepath 指向的目录中建立这两个文件夹
    # 比如产生: C:\Users\xin\Videos\source\videocompres 和 C:\Users\xin\Videos\source\origin
    cfg.out_dir_name = r"C:\Users\xin\Videos\videocompres\video"
    cfg.archive_dir_name = "origin"
    
    # 保持目录结构
    cfg.preserve_relative_path = True
    cfg.crf = 28
    
    # 执行智能模式
    run_smart_compression(cfg)