import os
import csv
import hashlib
import subprocess
from pathlib import Path
from tqdm import tqdm
# ========= 配置 =========
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".flv", ".webm"}

HEAD_SIZE = 1024 * 1024  # 1MB
SAMPLE_SIZE = 256 * 1024  # 256KB


# ========= hash工具 =========
def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get_head_hash(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read(HEAD_SIZE)
        return hash_bytes(data)
    except:
        return ""


def get_sample_hash(path: Path, file_size: int) -> str:
    try:
        with open(path, "rb") as f:
            if file_size < SAMPLE_SIZE * 3:
                data = f.read()
                return hash_bytes(data)

            # 取 3个采样点：10%, 50%, 90%
            points = [0.1, 0.5, 0.9]
            chunks = []

            for p in points:
                offset = int(file_size * p)
                f.seek(max(0, offset))
                chunks.append(f.read(SAMPLE_SIZE))

        return hash_bytes(b"".join(chunks))
    except:
        return ""


# ========= ffprobe =========
def get_video_meta(path: Path):
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,codec_name,bit_rate,r_frame_rate,duration",
            "-of", "default=noprint_wrappers=1",
            str(path),
        ]

        result = subprocess.run(cmd, capture_output=True)

        output = result.stdout.decode("utf-8", errors="ignore")

        meta = {
            "width": "",
            "height": "",
            "codec": "",
            "bitrate": "",
            "duration": "",
            "fps": ""
        }

        for line in output.splitlines():
            if "width=" in line:
                meta["width"] = line.split("=")[1].strip()
            elif "height=" in line:
                meta["height"] = line.split("=")[1].strip()
            elif "codec_name=" in line:
                meta["codec"] = line.split("=")[1].strip()
            elif "bit_rate=" in line:
                meta["bitrate"] = line.split("=")[1].strip()
            elif "r_frame_rate=" in line:
                meta["fps"] = line.split("=")[1].strip()
            elif "duration=" in line:
                meta["duration"] = line.split("=")[1].strip()

        return meta

    except:
        return {
            "width": "",
            "height": "",
            "codec": "",
            "bitrate": "",
            "duration": "",
            "fps": ""
        }
# ========= 主扫描 =========
def scan_folder(folder: Path):
    files_list = []

    for root, _, files in os.walk(folder):
        for name in files:
            path = Path(root) / name
            if path.suffix.lower() in VIDEO_EXTS:
                files_list.append(path)

    results = []

    for path in tqdm(files_list, desc="Scanning videos"):
        try:
            stat = path.stat()
            size = stat.st_size

            head_hash = get_head_hash(path)
            sample_hash = get_sample_hash(path, size)
            meta = get_video_meta(path)

            results.append({
                "path": str(path),
                "size": size,
                "mtime": int(stat.st_mtime),

                "head_hash": head_hash,
                "sample_hash": sample_hash,

                "width": meta["width"],
                "height": meta["height"],
                "codec": meta["codec"],
                "bitrate": meta["bitrate"],
                "duration": meta["duration"],
                "fps": meta["fps"],
            })

        except Exception as e:
            print(f"error: {path} -> {e}")

    return results

# ========= CSV输出 =========
def write_csv(data, output_path: Path):
    if not data:
        return

    keys = data[0].keys()

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(data)


# ========= main =========
if __name__ == "__main__":
    target_folder = Path(input("输入视频目录: ").strip()).resolve()

    if not target_folder.exists():
        print("目录不存在")
        exit(1)

    data = scan_folder(target_folder)

    output_csv = target_folder.parent / f"{target_folder.name}_video_index.csv"
    write_csv(data, output_csv)

    print(f"完成: {output_csv}")