import os
import subprocess
import shutil

# 指向你挂载的 NAS 音乐目录
MUSIC_DIR = input("请输入你的音乐库路径: ")

def batch_process_music(directory):
    target_files = []
    
    # 1. 扫描所有 mp3 和 MP3 文件
    print("🔍 正在扫描曲库...")
    for root, _, files in os.walk(directory):
        for file in files:
            ext = file.lower()
            # 扫描 mp3 用于转换，扫描 mp3 用于可能的去广告
            if ext.endswith('.mp3') or ext.endswith('.mp3'):
                target_files.append(os.path.join(root, file))
                
    if not target_files:
        print("🎉 你的曲库很干净，没有找到任何需要处理的文件！")
        return

    print(f"⚠️ 发现 {len(target_files)} 个音频文件，准备开始无缝处理...")

    # 2. 逐个处理
    for index, file_path in enumerate(target_files):
        filename = os.path.basename(file_path)
        base_name = os.path.splitext(file_path)[0]
        ext = os.path.splitext(file_path)[1].lower()
        
        # 目标最终路径（不管原格式是啥，最终都要变 mp3）
        final_mp3_path = f"{base_name}.mp3"
        
        # ✨ 引入过渡文件：防止 FFmpeg 报错 "same as Input"
        temp_mp3_path = f"{base_name}_temp_converting.mp3"
        
        # 是否需要去广告的标志
        is_melo = 'melo' in filename.lower()
        
        # 如果是 mp3 且不需要去广告，直接跳过，节省算力
        if ext == '.mp3' and not is_melo:
            continue
            
        print(f"[{index+1}/{len(target_files)}] 正在处理: {filename}")
        
        # 构建基础命令：先声明输入文件
        command = [
            'ffmpeg',
            '-i', file_path,
        ]
        
        # ================= 核心广告切除 =================
        if is_melo:
            # 将 -ss 放在 -i 后面，进行安全解码级跳过
            command.extend(['-ss', '22'])
            print("  └─ ✂️ 检测到 'melo'，正在精确切除前22秒广告...")
        # ============================================

        # 补全后续编码参数，输出到 【临时文件】
        command.extend([
            '-c:a', 'libmp3lame', 
            '-q:a', '2',          
            '-y',                 
            temp_mp3_path
        ])
        
        try:
            # 执行 FFmpeg，强制 UTF-8 捕获错误日志
            subprocess.run(
                command, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.PIPE, 
                text=True, 
                encoding='utf-8', 
                errors='ignore',
                check=True
            )
            
            # 3. 【出场处理】：安全替换文件
            # 如果原文件就是 mp3 或者原文件是 MP3，统统删掉原文件
            os.remove(file_path)
            # 把转好的临时 MP3 重命名为正式 MP3
            shutil.move(temp_mp3_path, final_mp3_path)
            
            if is_melo:
                print(f"  └─ ✅ 去广告完成，已安全覆盖。")
            else:
                print(f"  └─ ✅ 成功转为 MP3，已清理原文件。")
            
        except subprocess.CalledProcessError as e:
            # 如果报错，先把临时产生的垃圾文件删掉，防止污染曲库
            if os.path.exists(temp_mp3_path):
                os.remove(temp_mp3_path)
                
            error_lines = e.stderr.strip().split('\n')
            core_error = " | ".join(error_lines[-2:]) if len(error_lines) >= 2 else e.stderr
            print(f"  └─ ❌ 转换失败，FFmpeg报错: {core_error}")
            
        except Exception as e:
            if os.path.exists(temp_mp3_path):
                os.remove(temp_mp3_path)
            print(f"  └─ ❌ 发生系统异常: {e}")

    print("\n✨ 所有清理与去广告任务执行完毕！")

if __name__ == '__main__':
    batch_process_music(MUSIC_DIR)