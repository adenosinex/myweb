#!/usr/bin/env python3
"""
app.py — 知乎链接转文本 Web 服务

启动：
    python app.py                 # 默认 http://127.0.0.1:8000
    PORT=9000 python app.py       # 自定义端口

环境变量：
    ZHIHU_COOKIE      直接提供 Cookie 字符串（可选）
    ZHIHU_COOKIE_FILE Cookie 文件路径（可选，默认 cookies.txt）
    ZHIHU_BROWSER=1   使用浏览器模式（抗风控，较慢）
    ZHIHU_HEADLESS=0  浏览器模式下显示窗口

接口：
    GET    /api/health    服务与 Cookie 状态
    GET    /api/cookie    查看服务器本地 Cookie 状态（只回显打码预览）
    POST   /api/cookie    把 Cookie 写入服务器本地文件（默认 cookies.txt）
    DELETE /api/cookie    删除服务器本地 Cookie 文件
    POST   /api/extract   提取正文
"""

from __future__ import annotations

import os
import re
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import zhihu_extract as ze

APP_DIR = Path(__file__).parent.resolve()
DEFAULT_COOKIE_FILE = APP_DIR / "cookies.txt"

app = FastAPI(title="知乎链接转文本", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- 请求模型


class ExtractReq(BaseModel):
    url: str = Field(..., description="知乎链接")
    cookie: str = Field("", description="可选，本次请求使用的 Cookie")
    format: str = Field("markdown", description="markdown | text | json")


class ExtractResp(BaseModel):
    ok: bool
    kind: str = ""
    id: str = ""
    title: str = ""
    author: str = ""
    published: str = ""
    voteup: int = 0
    comment_count: int = 0
    url: str = ""
    content: str = ""
    images: list[str] = []
    error: str = ""


class CookieReq(BaseModel):
    cookie: str = Field("", description="完整 Cookie 请求头字符串")


# ---------------------------------------------------------------- Cookie 本地存取


def _cookie_file() -> Path:
    """服务器本地 Cookie 文件路径（可用 ZHIHU_COOKIE_FILE 覆盖）。"""
    return Path(os.environ.get("ZHIHU_COOKIE_FILE", str(DEFAULT_COOKIE_FILE)))


def normalize_cookie(raw: str) -> str:
    """把粘贴进来的内容整理成单行 Cookie 请求头。

    容忍多行粘贴、「Cookie:」前缀、多余空格与行尾分号。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?im)^\s*cookie\s*:\s*", "", s)
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s*;\s*", "; ", s)
    return s.strip().strip(";").strip()


def missing_fields(cookie: str) -> list[str]:
    """返回缺失的必需字段（知乎登录态至少要 d_c0 与 z_c0）。"""
    return [k for k in ("d_c0", "z_c0")
            if not re.search(r"(?:^|;\s*)" + k + r"=", cookie)]


def mask_cookie(cookie: str) -> str:
    """只回显关键字段的前 6 位，避免接口把完整凭证原样吐回去。"""
    if not cookie.strip():
        return ""
    parts = []
    for key in ("d_c0", "z_c0"):
        m = re.search(r"(?:^|;\s*)" + key + r"=([^;]*)", cookie)
        val = m.group(1).strip() if m else ""
        parts.append(f"{key}=—" if not val else f"{key}={val[:6]}…({len(val)}字符)")
    return " · ".join(parts)


def stored_cookie() -> str:
    """读取服务器本地 Cookie 文件；不存在或格式损坏时返回空串。"""
    path = _cookie_file()
    if not path.exists():
        return ""
    try:
        return ze.load_cookie(str(path)).strip()
    except (ze.ZhihuError, OSError, ValueError):
        return ""


def _restrict(path: Path) -> None:
    """尽量收紧文件权限（Windows 上 chmod 作用有限，失败不影响功能）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _resolve_cookie(req_cookie: str) -> str:
    """优先级：请求参数 > ZHIHU_COOKIE 环境变量 > 服务器本地 cookies.txt。"""
    if req_cookie.strip():
        return req_cookie.strip()
    env_cookie = os.environ.get("ZHIHU_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    return stored_cookie()


def _use_browser() -> bool:
    return os.environ.get("ZHIHU_BROWSER", "").strip() in ("1", "true", "yes")


# ---------------------------------------------------------------- 接口


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((APP_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> dict:
    stored = stored_cookie()
    env_set = bool(os.environ.get("ZHIHU_COOKIE", "").strip())
    cookie_ready = bool(stored or env_set)
    return {
        "ok": True,
        "browser_mode": _use_browser(),
        "cookie_configured": cookie_ready,
        "cookie_source": "env" if env_set else ("file" if stored else ""),
        "cookie_file": str(_cookie_file()),
        "cookie_masked": mask_cookie(stored),
        "hint": ("已就绪" if cookie_ready or _use_browser()
                 else "尚未配置 Cookie：请在页面末尾「知乎 Cookie」中保存，"
                      "或放置 cookies.txt，或设置 ZHIHU_BROWSER=1"),
    }


@app.post("/api/extract", response_model=ExtractResp)
def api_extract(req: ExtractReq) -> ExtractResp:
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="请提供知乎链接")
    if "zhihu.com" not in url:
        raise HTTPException(status_code=400, detail="请提供 zhihu.com 域名的链接")

    cookie = _resolve_cookie(req.cookie)
    use_browser = _use_browser()

    if not cookie and not use_browser:
        return ExtractResp(
            ok=False,
            error="服务端未配置 Cookie。请在页面末尾「知乎 Cookie」中粘贴并点「保存」"
                  "（会写入服务器本地 cookies.txt），"
                  "或直接把 cookies.txt 放到项目目录，"
                  "或设置环境变量 ZHIHU_BROWSER=1 改用浏览器模式。",
        )

    try:
        doc = ze.extract(
            url,
            cookie=cookie,
            use_browser=use_browser,
            headless=os.environ.get("ZHIHU_HEADLESS", "1") != "0",
            profile_dir=str(APP_DIR / ".zhihu-profile"),
            verbose=False,
        )
    except ze.ZhihuError as e:
        return ExtractResp(ok=False, error=str(e))
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return ExtractResp(ok=False, error=f"服务器内部错误：{type(e).__name__}: {e}")

    fmt = req.format.lower()
    if fmt == "text":
        content = doc.text
    elif fmt == "json":
        import json
        content = json.dumps(doc.to_dict(), ensure_ascii=False, indent=2)
    else:
        content = doc.markdown

    return ExtractResp(
        ok=True, kind=doc.kind, id=doc.id, title=doc.title, author=doc.author,
        published=doc.published, voteup=doc.voteup,
        comment_count=doc.comment_count, url=doc.url,
        content=content, images=doc.images,
    )


@app.get("/api/extract")
def api_extract_get(url: str, format: str = "markdown",
                    cookie: str = "") -> JSONResponse:
    """便捷 GET 接口：/api/extract?url=<链接>&format=text"""
    resp = api_extract(ExtractReq(url=url, format=format, cookie=cookie))
    return JSONResponse(content=resp.model_dump())


# ---------------------------------------------------------------- Cookie 接口


@app.get("/api/cookie")
def api_get_cookie() -> dict:
    """查看服务器本地 Cookie 状态。

    只返回打码预览，不回传完整凭证——页面本身不需要完整 Cookie，
    后端会在提取时直接读取本地文件。
    """
    path = _cookie_file()
    stored = stored_cookie()
    env_cookie = os.environ.get("ZHIHU_COOKIE", "").strip()
    effective = stored or env_cookie
    return {
        "ok": True,
        "configured": bool(effective),
        "source": "env" if env_cookie else ("file" if stored else ""),
        "file": str(path),
        "file_exists": path.exists(),
        "masked": mask_cookie(stored),
        "missing": missing_fields(effective) if effective else ["d_c0", "z_c0"],
        "env_override": bool(env_cookie),
    }


@app.post("/api/cookie")
def api_set_cookie(req: CookieReq) -> dict:
    """把 Cookie 写入服务器本地文件，之后的提取会自动使用它。"""
    cookie = normalize_cookie(req.cookie)
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")

    miss = missing_fields(cookie)
    if miss:
        raise HTTPException(
            status_code=400,
            detail="Cookie 缺少必要字段：" + "、".join(miss)
                   + "（请从已登录的浏览器完整复制整行 Cookie）",
        )

    path = _cookie_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(cookie + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)          # 原子替换，避免写坏已有文件
        _restrict(path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"写入失败：{e}") from e

    return {
        "ok": True,
        "file": str(path),
        "source": "file",
        "masked": mask_cookie(cookie),
        "hint": "已保存到服务器本地，后续提取会自动使用",
    }


@app.delete("/api/cookie")
def api_delete_cookie() -> dict:
    """删除服务器本地的 Cookie 文件。"""
    path = _cookie_file()
    if not path.exists():
        return {"ok": True, "removed": False, "file": str(path),
                "hint": "服务器本地没有 Cookie 文件"}
    try:
        path.unlink()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除失败：{e}") from e
    return {"ok": True, "removed": True, "file": str(path),
            "hint": "已删除服务器本地 Cookie 文件"}


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8320"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n  知乎链接转文本服务已启动 →  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
