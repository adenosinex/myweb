import os
import subprocess

# 指向你挂载的 NAS 音乐目录
MUSIC_DIR =input("wma:")

def batch_convert_wma_to_mp3(directory):
    wma_files = []
    
    # 1. 扫描所有 WMA 文件
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith('.wma'):
                wma_files.append(os.path.join(root, file))
                
    if not wma_files:
        print("🎉 你的曲库很干净，没有找到任何 WMA 文件！")
        return

    print(f"⚠️ 发现 {len(wma_files)} 个 WMA 文件，准备开始无缝转换...")

    # 2. 逐个转换
    for index, wma_path in enumerate(wma_files):
        # 拆分文件名，替换后缀：例如 "song.wma" -> "song.mp3"
        base_name = os.path.splitext(wma_path)[0]
        mp3_path = f"{base_name}.mp3"
        
        print(f"[{index+1}/{len(wma_files)}] 正在转换: {os.path.basename(wma_path)}")
        
        # 构建 FFmpeg 命令：使用 libmp3lame 编码器，-q:a 2 代表高音质可变比特率 (VBR)
        command = [
            'ffmpeg',
            '-i', wma_path,       # 输入文件
            '-c:a', 'libmp3lame', # 指定 MP3 编码器
            '-q:a', '2',          # 音质等级 (0-9，2 是非常高的质量)
            '-y',                 # 如果 mp3 已存在则覆盖
            mp3_path
        ]
        
        try:
            # 隐藏 FFmpeg 的冗长输出，只捕获错误
            subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            
            # 3. 转换成功后，删除原 WMA 文件（腾出 NAS 空间）
            os.remove(wma_path)
            print(f"  └─ ✅ 成功转为 MP3，已清理原文件。")
            
        except subprocess.CalledProcessError:
            print(f"  └─ ❌ 转换失败，请检查文件是否损坏: {wma_path}")
        except Exception as e:
            print(f"  └─ ❌ 发生异常: {e}")

    print("\n✨ 所有 WMA 清理任务执行完毕！")

if __name__ == '__main__':
    batch_convert_wma_to_mp3(MUSIC_DIR)