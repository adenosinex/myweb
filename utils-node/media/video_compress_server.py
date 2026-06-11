import uuid
import subprocess
import os
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote

APP_ROOT = Path.cwd()
OUT_ROOT = APP_ROOT / "outputs"
OUT_ROOT.mkdir(exist_ok=True)

app = FastAPI(title="Simple Video Compress Server")
app.mount("/outputs", StaticFiles(directory=str(OUT_ROOT)), name="outputs")

@app.get("/")
def index():
    return {"status": "Server is running. Upload videos via raw binary stream to /upload."}

# 核心优化 4：改写为接收二进制裸流，彻底剥离 UploadFile 解析负担
@app.post("/upload")
async def upload(
    request: Request, 
    filename: str = "video.mp4",
    target_resolution: int = 1080, 
    crf: int = 22,
    preset: str = "medium"
):
    unique_id = uuid.uuid4().hex[:8]
    ext = "".join(Path(filename).suffixes) or ".mp4"
    input_path = OUT_ROOT / f"in_{unique_id}{ext}"
    output_path = OUT_ROOT / f"out_{unique_id}.mp4"

    # 核心优化 5：通过 request.stream() 直接获取异步字节流并落盘
    with input_path.open("wb") as f:
        async for chunk in request.stream():
            f.write(chunk)

    max_dim = int(target_resolution * 16 / 9)
    scale_filter = (
        f"scale='min({max_dim},iw)':'min({max_dim},ih)':force_original_aspect_ratio=decrease,"
        f"pad=ceil(iw/2)*2:ceil(ih/2)*2"
    )

    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", scale_filter,
        "-c:v", "libx265",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", "yuv420p10le",
        "-x265-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-c:a", "aac", "-b:a", "128k",
        "-progress", "pipe:2",
        "-nostats",
        str(output_path)
    ]

    def iter_stream():
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            for line in p.stderr:
                try:
                    text = line.decode("utf-8", errors="ignore").strip()
                except Exception:
                    text = line.decode(errors="ignore").strip()
                
                if text:
                    yield (f"PROGRESS: {text}\n").encode("utf-8")
            
            rc = p.wait()
            yield (f"PROGRESS: ffmpeg exitcode={rc}\n").encode("utf-8")

            if rc == 0 and output_path.exists():
                yield b"===FILE-START===\n"
                with output_path.open("rb") as fout:
                    while True:
                        # 服务端回传同样使用 1MB 大块
                        chunk = fout.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk
            else:
                yield b"PROGRESS: ERROR transcode failed\n"
        finally:
            if p and p.poll() is None:
                p.kill()
            
            for path in [input_path, output_path]:
                if path.exists():
                    try:
                        os.remove(path)
                    except Exception:
                        pass

    encoded_filename = quote(str(filename))
    headers = {"Content-Disposition": f"attachment; filename={encoded_filename}"}
    return StreamingResponse(iter_stream(), media_type="application/octet-stream", headers=headers)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)