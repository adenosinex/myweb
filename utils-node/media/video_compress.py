from datetime import datetime
import os
import sys
import subprocess
import requests
import json
import shutil
import traceback
import time
import tempfile

try:
    import cv2
except ImportError:
    cv2 = None
    print("⚠️ 缺少 opencv-python 库，智能分析模式将受限。如需使用智能模式请执行: pip install opencv-python")


# ══════════════════════════════════════════════════════════════════════════════
# [改] 终端 ANSI 初始化 + 颜色降级
# ══════════════════════════════════════════════════════════════════════════════
def _init_terminal():
    width = 80
    try:
        width = os.get_terminal_size().columns
    except Exception:
        pass
    if not hasattr(sys.stdout, 'isatty') or not sys.stdout.isatty():
        return False, width
    if os.name != 'nt':
        return True, width
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)
        m = ctypes.c_ulong()
        k32.GetConsoleMode(h, ctypes.byref(m))
        if not (m.value & 0x0004):
            k32.SetConsoleMode(h, m.value | 0x0004)
        return True, width
    except Exception:
        return False, width


_ANSI, _TW = _init_terminal()


def _c(s):
    return s if _ANSI else ""

CR = _c("\033[0m")
CG = _c("\033[32m")
CY = _c("\033[33m")
CC = _c("\033[36m")
CM = _c("\033[35m")
CW = _c("\033[97m")
CGr = _c("\033[90m")
CB = _c("\033[1m")


# [改] 进度行原地刷新
def _inline(text):
    if _ANSI:
        sys.stdout.write(f"\r\033[K{text}")
    else:
        sys.stdout.write(f"\r{' ' * min(_TW, 200)}\r{text}")
    sys.stdout.flush()


def _inline_done():
    sys.stdout.write("\n")
    sys.stdout.flush()


# [改] 进度条 + 速度 + ETA
def _bar(r, w=22):
    f = int(r * w)
    return "█" * f + "░" * (w - f)


def _prog(label, pct, nbytes, elapsed):
    b = _bar(pct / 100)
    spd = nbytes / elapsed if elapsed > 0 else 0
    eta = (elapsed / pct * (100 - pct)) if pct > 0 else 0
    return (f"  {label} {b} {CW}{pct:5.1f}%{CR}"
            f"  {_sz(nbytes)}  {CC}{_sz(spd)}/s{CR}  ETA {CY}{_eta(eta)}{CR}")


# [改] 格式化工具
def _sz(b):
    if b < 1024: return f"{b}B"
    if b < 1048576: return f"{b/1024:.1f}KB"
    if b < 1073741824: return f"{b/1048576:.1f}MB"
    return f"{b/1073741824:.2f}GB"


def _eta(s):
    if s < 0 or s > 86400: return "--:--"
    m, sc = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sc:02d}" if h else f"{m}:{sc:02d}"


def _dur(s):
    m, sc = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sc:02d}" if h else f"{m}:{sc:02d}"


# [改] 批次总进度
def _batch_bar(idx, total, comp, skip):
    pct = (idx / total * 100) if total > 0 else 0
    b = _bar(pct / 100, 18)
    print(f"  {CC}[总进度]{CR} [{idx}/{total}] {b} {CW}{pct:.0f}%{CR}  压缩:{comp} 跳过:{skip}")


# [改] x265 信息紧凑解析
def _x265_key(line):
    if "x265 [info]" not in line:
        return None
    t = line.split("x265 [info]")[-1].strip().rstrip('.')
    if t.startswith("HEVC encoder version"):
        return ("enc", t.split("version")[-1].strip())
    if "pool features" in t:
        return ("thr", t.split("/")[-1].strip().rstrip(')'))
    if t.startswith("Main profile") or t.startswith("Main 10"):
        return ("prof", t)
    if "Rate Control" in t:
        p = [x.strip() for x in t.split("/") if "CRF" in x.upper()]
        return ("crf", p[0] if p else t)
    if "encoded" in t and "frames" in t:
        return ("sum", t)
    return None


def _print_x265(d):
    if not d: return
    p = []
    if "enc" in d:  p.append(f"x265 {d['enc']}")
    if "prof" in d:  p.append(d['prof'])
    if "thr" in d:   p.append(f"线程:{d['thr']}")
    if "crf" in d:   p.append(d['crf'])
    if p:
        print(f"  {CM}⚙  {' | '.join(p)}{CR}")


# ══════════════════════════════════════════════════════════════════════════════
# 1. 集中化参数配置类（原样保留）
# ══════════════════════════════════════════════════════════════════════════════
class CompressConfig:
    def __init__(self):
        self.filepath = './'
        self.server_url = "http://apple4.su7.dpdns.org:8005/upload"
        self.target_res = 1080
        self.skip_bitrate_kbps = 3000
        self.crf = 28
        self.preset = "medium"
        self.out_dir_name = "videocompres"
        self.archive_dir_name = "origin"
        self.preserve_relative_path = True
        self.move_skipped_files = False


# 全局统计数据（原样保留）
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


# ══════════════════════════════════════════════════════════════════════════════
# get_video_info（原样保留）
# ══════════════════════════════════════════════════════════════════════════════
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
        print(f"  {CY}⚠  无法解析视频元数据 {os.path.basename(filepath)}: {e}{CR}")
        return 0.0, float('inf'), float('inf'), '', 30.0, 0


# ══════════════════════════════════════════════════════════════════════════════
# smart_analyze_video（原逻辑保留，仅 print 改用颜色）
# ══════════════════════════════════════════════════════════════════════════════
def smart_analyze_video(filepath, duration, original_size, codec, fps, config: CompressConfig):
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
                print(f"    {CGr}· 画面运动量极低 (PPT/监控等){CR}")
                target_fps = 30.0
            else:
                target_fps = fps if fps < 60 else 60.0
            if avg_complexity < 5.0:
                print(f"    {CGr}· 场景复杂度低 (动漫/纯色){CR}")
                adjusted_crf = config.crf + 2
            elif avg_complexity > 30.0:
                print(f"    {CGr}· 场景复杂度高 (树叶/雪景){CR}")
                adjusted_crf = config.crf - 2
        except Exception as e:
            print(f"    {CY}⚠ OpenCV 抽样失败，使用默认参数 ({e}){CR}")

    if original_size > 500 * 1024 * 1024 and duration >= 10:
        print(f"    {CGr}· 触发大文件 5s 极速试压检测...{CR}")
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
            subprocess.run(sample_cmd, timeout=30,
                           stderr=subprocess.DEVNULL,   # ← 唯一改动：吞掉 x265 全部输出
                           stdout=subprocess.DEVNULL)   # ← 顺手
            if os.path.exists(temp_out):
                sample_size = os.path.getsize(temp_out)
                os.remove(temp_out)
                orig_5s_size = original_size * (5.0 / duration)
                sample_ratio = sample_size / orig_5s_size if orig_5s_size > 0 else 1.0
                print(f"    {CGr}· 试压比率: {sample_ratio:.2f}{CR}")
                if sample_ratio > 0.9:
                    return True, "样本试压测试显示收益不足10%", adjusted_crf, target_fps
        except Exception:
            print(f"    {CY}⚠ 试压超时或失败，跳过试压判断{CR}")
            if os.path.exists(temp_out):
                os.remove(temp_out)

    return False, "", adjusted_crf, target_fps


# ══════════════════════════════════════════════════════════════════════════════
# compress_stream —— 压缩逻辑原样保留，仅替换输出层
# ══════════════════════════════════════════════════════════════════════════════
def _update_pbar_from_text(text, duration, last_progress):
    """[改] 去掉 tqdm 依赖，只返回 (新进度百分比, 是否更新)"""
    if "out_time_us=" in text and duration > 0:
        try:
            out_time_us = int(text.split("=")[1])
            current_sec = out_time_us / 1_000_000
            prog = min(100.0, (current_sec / duration) * 100)
            if prog > last_progress:
                return prog, True
        except Exception:
            pass
    return last_progress, False


def compress_stream(input_path, output_path, dst_archive, duration,
                    target_crf, target_fps, config: CompressConfig):
    """原逻辑完全保留，仅把 tqdm 替换为 _inline 刷新"""
    filename = os.path.basename(input_path)

    marker = b"===FILE-START===\n"
    buffer = bytearray()
    file_started = False
    last_progress = 0.0

    # [改] x265 紧凑信息收集
    x265_info = {}
    x265_printed = False
    t_start = time.time()

    params = {
        'filename': filename,
        'target_resolution': config.target_res,
        'crf': target_crf,
        'preset': config.preset,
        'fps': target_fps
    }

    original_size = os.path.getsize(input_path)

    try:
        with open(input_path, 'rb') as f_in:
            with requests.post(config.server_url, params=params, data=f_in,
                               stream=True, timeout=86400) as response:
                response.raise_for_status()

                with open(output_path, 'wb') as fout:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not file_started:
                            buffer.extend(chunk)
                            idx = buffer.find(marker)

                            if idx != -1:
                                # ── 找到文件起始标记 ──
                                file_started = True
                                text_data = buffer[:idx].decode('utf-8', errors='ignore')

                                # [改] 解析标记前的剩余文本
                                for line in text_data.split('\n'):
                                    line = line.strip()
                                    if not line:
                                        continue
                                    pk = _x265_key(line)
                                    if pk:
                                        x265_info[pk[0]] = pk[1]
                                    else:
                                        last_progress, _ = _update_pbar_from_text(
                                            line, duration, last_progress)

                                # [改] 打印编码器信息
                                if not x265_printed and x265_info:
                                    _inline_done()
                                    _print_x265(x265_info)
                                    x265_printed = True

                                # [改] 切换到接收阶段
                                _inline_done()
                                print(f"  {CG}⏳ 压缩完成，正在接收文件...{CR}")

                                fout.write(buffer[idx + len(marker):])
                                buffer.clear()
                            else:
                                # ── 未找到标记，逐行解析进度 ──
                                lines = buffer.split(b'\n')
                                for line in lines[:-1]:
                                    text = line.decode('utf-8', errors='ignore')

                                    # [改] x265 紧凑解析（替代直接丢弃）
                                    pk = _x265_key(text)
                                    if pk:
                                        x265_info[pk[0]] = pk[1]
                                        continue

                                    # 原始进度解析逻辑
                                    last_progress, updated = _update_pbar_from_text(
                                        text, duration, last_progress)

                                    # [改] 用 _inline 替代 tqdm
                                    if updated:
                                        elapsed = time.time() - t_start
                                        est = int(last_progress / 100 * original_size)
                                        _inline(_prog("压缩中  ", last_progress, est, elapsed))

                                    # [改] 抓取编码速度
                                    if "speed=" in text:
                                        try:
                                            spd = text.split("speed=")[1].strip().rstrip('x')
                                            if spd and spd != 'N/A':
                                                x265_info['speed'] = spd
                                        except Exception:
                                            pass

                                buffer = bytearray(lines[-1])
                        else:
                            # ── 文件数据阶段（原样保留） ──
                            fout.write(chunk)

    except requests.exceptions.RequestException as e:
        _inline_done()
        print(f"\n  {CC}❌ 网络请求失败或服务端中断: {e}{CR}")
        if getattr(e, 'response', None) is not None:
            print(f"  服务端返回状态码: {e.response.status_code}")
        if os.path.exists(output_path):
            os.remove(output_path)
        raise e

    if file_started:
        # [改] 汇总行替代原 print
        elapsed_total = time.time() - t_start
        if not x265_printed and x265_info:
            _inline_done()
            _print_x265(x265_info)
        compressed_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        ratio = compressed_size / original_size if original_size > 0 else 0
        saved = original_size - compressed_size
        spd = x265_info.get('speed', '--')
        _inline_done()
        print(f"  {CG}✅ 接收完成{CR}  "
              f"压缩率 {CW}{ratio*100:.1f}%{CR}"
              f"  节省 {CG}{_sz(saved)}{CR}"
              f"  编码速度 {spd}x"
              f"  耗时 {_dur(elapsed_total)}")
    else:
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError("服务端未能成功返回视频流数据（可能转码器报错）。")


# ══════════════════════════════════════════════════════════════════════════════
# _core_process_pipeline（原逻辑保留，仅 print 改用颜色）
# ══════════════════════════════════════════════════════════════════════════════
def _core_process_pipeline(config: CompressConfig, use_smart_mode: bool):
    target_path = os.path.abspath(config.filepath)
    if not os.path.exists(target_path):
        print(f"❌ 无效的输入路径: {target_path}")
        return

    base_input_dir = target_path if os.path.isdir(target_path) else os.path.dirname(target_path)
    base_out_dir = os.path.join(base_input_dir, config.out_dir_name) if config.out_dir_name else base_input_dir
    base_archive_dir = os.path.join(base_input_dir, config.archive_dir_name) if config.archive_dir_name else base_input_dir

    suffix = "_compressed"
    files = []

    if os.path.isfile(target_path):
        if target_path.lower().endswith(('.mp4', '.mov', '.m4v')):
            name, ext = os.path.splitext(target_path)
            if not name.endswith(suffix):
                files = [target_path]
    elif os.path.isdir(target_path):
        for root, dirs, filenames in os.walk(target_path):
            if base_out_dir in os.path.abspath(root) or base_archive_dir in os.path.abspath(root):
                continue
            for f in filenames:
                if f.lower().endswith(('.mp4', '.mov', '.m4v')):
                    name, ext = os.path.splitext(f)
                    if name.endswith(suffix):
                        continue
                    files.append(os.path.join(root, f))

        if not files:
            print("目录及其有效子目录中没有找到需要处理的视频文件。")
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

            if config.preserve_relative_path:
                rel_path = os.path.relpath(file_dir, base_input_dir)
                if rel_path == '.':
                    rel_path = ''
            else:
                rel_path = ''

            current_out_dir = os.path.join(base_out_dir, rel_path) if rel_path else base_out_dir
            os.makedirs(current_out_dir, exist_ok=True)
            output_path = os.path.join(current_out_dir, f"{name}{suffix}{ext}")

            skip_dir_base = os.path.join(base_archive_dir, "skip")
            success_dir_base = base_archive_dir

            skip_archive = os.path.join(skip_dir_base, rel_path) if rel_path else skip_dir_base
            dst_archive = os.path.join(success_dir_base, rel_path) if rel_path else success_dir_base

            def archive_skipped_file():
                if config.move_skipped_files:
                    os.makedirs(skip_archive, exist_ok=True)
                    target_file = os.path.join(skip_archive, filename)
                    if os.path.abspath(vf) != os.path.abspath(target_file):
                        try:
                            shutil.move(vf, target_file)
                        except:
                            pass

            def archive_success_file():
                os.makedirs(dst_archive, exist_ok=True)
                target_file = os.path.join(dst_archive, filename)
                if os.path.abspath(vf) != os.path.abspath(target_file):
                    try:
                        shutil.move(vf, target_file)
                    except:
                        pass

            # [改] 文件头输出
            print()
            _batch_bar(idx, len(files), TaskStats.compressed_files, TaskStats.skipped_files)
            print(f"  {CC}📂 [{idx+1}/{len(files)}]{CR}  "
                  f"{CW}{filename}{CR}  ({_sz(original_size)})")

            if os.path.exists(output_path):
                print(f"  {CY}⏭  目标输出文件已存在，无需重复压缩{CR}")
                TaskStats.skipped_files += 1
                archive_skipped_file()
                continue

            duration, bitrate_kbps, height, codec, fps, size = get_video_info(vf)

            # [改] 元数据紧凑行
            res_tag = f"{height}p" if height > 0 else "?"
            print(f"  {CGr}    {codec.upper()} | {res_tag} | "
                  f"{bitrate_kbps:.0f}kbps | {fps:.1f}fps | {_dur(duration)}{CR}")

            if height <= config.target_res and 0 < bitrate_kbps <= config.skip_bitrate_kbps:
                print(f"  {CY}⏭  分辨率/码率极低，无需处理 | 码率: {bitrate_kbps:.0f} kbps{CR}")
                TaskStats.skipped_files += 1
                archive_skipped_file()
                continue

            if use_smart_mode:
                t_analyze_start = time.time()
                should_skip, skip_reason, crf_final, fps_final = smart_analyze_video(
                    vf, duration, size, codec, fps, config
                )
                t_analyze = time.time() - t_analyze_start

                # [改] 分析结果紧凑行
                fps_note = f"→{fps_final:.0f}fps" if abs(fps_final - fps) > 1 else f"{fps:.1f}fps"
                print(f"  {CG}💡 分析完成{CR}  "
                      f"{CGr}CRF {crf_final} | {fps_note} | "
                      f"原码率 {bitrate_kbps:.0f}kbps | {codec} | {t_analyze:.1f}s{CR}")

                if should_skip:
                    print(f"  {CY}⏭  跳过: {skip_reason}{CR}")
                    TaskStats.skipped_files += 1
                    archive_skipped_file()
                    continue
            else:
                crf_final = config.crf
                fps_final = fps
                print(f"  {CGr}⚙  机械模式: 固定 CRF {crf_final} | 目标帧率 {fps_final:.0f}fps{CR}")

            t_compress_start = time.time()
            compress_stream(vf, output_path, dst_archive, duration, crf_final, fps_final, config)
            t_compress = time.time() - t_compress_start

            if os.path.exists(output_path):
                compressed_size = os.path.getsize(output_path)
                if compressed_size >= original_size:
                    print(f"  {CY}⚠  压缩后体积未减小 "
                          f"({_sz(compressed_size)} >= {_sz(original_size)})，已舍弃废件{CR}")
                    os.remove(output_path)
                    TaskStats.skipped_files += 1
                    archive_skipped_file()
                else:
                    TaskStats.compressed_files += 1
                    TaskStats.total_original_size += original_size
                    TaskStats.total_compressed_size += compressed_size
                    TaskStats.total_process_time += t_compress
                    archive_success_file()
                    ratio = compressed_size / original_size
                    print(f"  {CG}📊 原 {_sz(original_size)} → "
                          f"{_sz(compressed_size)} | "
                          f"压缩率 {ratio*100:.1f}% | "
                          f"耗时 {t_compress:.1f}s{CR}")

        except Exception:
            print(f"\n  {CC}❌ 处理文件 {os.path.basename(vf)} 时发生异常:{CR}")
            print(f"  {CGr}{traceback.format_exc().strip()}{CR}")
            print(f"  {CY}⏭  自动跳过该文件，继续处理队列中的下一个视频...{CR}")

    # [改] 汇总输出
    total_elapsed = time.time() - global_start_time
    mode_name = "智能分析压缩" if use_smart_mode else "机械化指定画质压缩"

    print()
    print(f"  {'━' * 50}")
    print(f"  {CB}📈 批量处理任务结束汇总 ({mode_name}){CR}")
    print(f"  {'━' * 50}")
    print(f"  计划处理数 (剔除已处理): {TaskStats.total_files}")
    print(f"  {CG}成功有效压缩: {TaskStats.compressed_files}{CR}")
    print(f"  {CY}无需或无效压缩被跳过: {TaskStats.skipped_files}{CR}")

    if TaskStats.compressed_files > 0:
        saved_space = TaskStats.total_original_size - TaskStats.total_compressed_size
        avg_ratio = TaskStats.total_compressed_size / TaskStats.total_original_size
        print(f"  {CG}为您节省空间: {_sz(saved_space)}{CR}")
        print(f"  实际平均压缩率: {avg_ratio*100:.1f} %")
    else:
        print(f"  节省空间总量: 0 MB")
        print(f"  实际平均压缩率: N/A")

    print(f"  总处理耗时: {_dur(total_elapsed)}")
    print(f"  {'━' * 50}")


def run_smart_compression(config: CompressConfig):
    print(f"\n{CC}🚀 启动【智能模式】压缩任务...{CR}")
    _core_process_pipeline(config, use_smart_mode=True)


def run_fixed_compression(config: CompressConfig):
    print(f"\n{CC}🚀 启动【机械模式】压缩任务...{CR}")
    _core_process_pipeline(config, use_smart_mode=False)


if __name__ == "__main__":
    print(f"  终端 ANSI: {'✅ 已启用' if _ANSI else '❌ 未启用 (降级为纯文本)'}  列宽: {_TW}")

    cfg = CompressConfig()
    cfg.filepath = input('cmp: ')
    cfg.out_dir_name = r"C:\Users\xin\Videos\videocompres\video"
    cfg.archive_dir_name = "origin"
    cfg.preserve_relative_path = True
    cfg.move_skipped_files = False
    cfg.crf = 28
    run_smart_compression(cfg)
