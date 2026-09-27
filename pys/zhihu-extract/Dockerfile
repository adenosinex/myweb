FROM python:3.11-slim

WORKDIR /app

# 国内网络可换成清华镜像加速；海外部署可留空使用官方源
ARG PIP_INDEX_URL=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 若需要浏览器模式（--browser / ZHIHU_BROWSER=1），取消下面注释。
# 镜像会增大约 400MB，但抗风控能力最强，且无需手动导出 Cookie。
# RUN pip install --no-cache-dir playwright \
#     && playwright install --with-deps chromium

COPY app.py index.html zhihu_extract.py ./

# Cookie 通过环境变量或挂载卷提供；不要把 cookies.txt 打进镜像
ENV ZHIHU_COOKIE_FILE=/app/data/cookies.txt \
    PORT=8000 \
    HOST=0.0.0.0
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["python", "app.py"]
