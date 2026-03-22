import shutil
import uuid
import subprocess
import os
from pathlib import Path
from fastapi import FastAPI, UploadFile, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from urllib.parse import quote

APP_ROOT = Path.cwd()
OUT_ROOT = APP_ROOT / "outputs"
OUT_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="Simple Video Compress Server")
app.mount("/outputs", StaticFiles(directory=str(OUT_ROOT)), name="outputs")

TEMPLATES_DIR = APP_ROOT / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

@app.get("/")
def index(request: Request):
    html_file = TEMPLATES_DIR / "index.html"
    if not html_file.exists():
        html_file.write_text("""
        <html>
        <body>
        <h3>上传视频压缩测试</h3>
        <form action="/upload" enctype="multipart/form-data" method="post">
          <input type="file" name="file"><br><br>
          <input type="submit" value="上传并压缩">
        </form>
        </body>
        </html>
        """, encoding="utf-8")
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/upload")
def upload(file: UploadFile):
    # 使用 UUID 杜绝并发上传时的文件覆盖问题
    unique_id = uuid.uuid4().hex[:8]
    ext = "".join(Path(file.filename).suffixes) or ".mp4"
    input_path = OUT_ROOT / f"in_{unique_id}{ext}"
    output_path = OUT_ROOT / f"out_{unique_id}.mp4"

    # 保存上传文件
    with input_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # 兼容性最高的硬件加速转码命令
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # 强制偶数分辨率，防报错
        "-c:v", "hevc_videotoolbox", "-b:v", "35M",   # 硬件加速与合理码率
        "-c:a", "aac", "-b:a", "128k",                # 统一音频流规范
        "-progress", "pipe:2",                        # 强制将机器进度发送到 stderr
        "-nostats",                                   # 屏蔽杂乱的原生日志
        str(output_path)
    ]

    def iter_stream():
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # 持续读取 stderr 输出进度给客户端
            for line in p.stderr:
                try:
                    text = line.decode("utf-8", errors="ignore").strip()
                except Exception:
                    text = line.decode(errors="ignore").strip()
                
                if text:
                    yield (f"PROGRESS: {text}\n").encode("utf-8")
            
            rc = p.wait()
            yield (f"PROGRESS: ffmpeg exitcode={rc}\n").encode("utf-8")

            # 编码成功且文件存在，发送隔离符并抛出二进制文件
            if rc == 0 and output_path.exists():
                yield b"===FILE-START===\n"
                with output_path.open("rb") as fout:
                    while True:
                        chunk = fout.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
            else:
                yield b"PROGRESS: ERROR transcode failed\n"
        finally:
            if p and p.poll() is None:
                p.kill()
            
            # 无论成功与否，强清理临时文件，确保存储 0 压力
            for path in [input_path, output_path]:
                if path.exists():
                    try:
                        os.remove(path)
                    except Exception:
                        pass

    encoded_filename = quote(str(file.filename))
    headers = {"Content-Disposition": f"attachment; filename={encoded_filename}"}
    return StreamingResponse(iter_stream(), media_type="application/octet-stream", headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)