import os
import sys
import time
import requests
import subprocess

CLOUD_SERVER_URL = "http://192.168.31.197:8100/dyfn/skip/api/get_latest_code"
MAIN_SCRIPT = "app.py"

def download_latest_code():
    print(f"[*] 启动引导程序，拉取云端节点: {CLOUD_SERVER_URL}")
    try:
        response = requests.get(CLOUD_SERVER_URL, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") != "success":
                print(f"[!] 云端返回错误: {data.get('error', '未知错误')}")
                return False
            
            files = data.get("files", {})
            for rel_path, content in files.items():
                # 核心：无视云端路径，按后缀强制重命名落地
                if rel_path.endswith('.py'):
                    local_filename = "app.py"
                elif rel_path.endswith('.html'):
                    local_filename = "index.html"
                else:
                    local_filename = os.path.basename(rel_path)
                
                with open(local_filename, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"[*] 更新成功: {rel_path} -> {local_filename}")
            return True
        else:
            print(f"[!] 云端服务器异常，状态码: {response.status_code}")
            return False
    except Exception as e:
        print(f"[!] 网络连接失败，跳过更新。原因: {e}")
        return False

def start_app():
    print("-" * 50)
    if not os.path.exists(MAIN_SCRIPT):
        print(f"[!] 致命错误：本地无可用代码 ({MAIN_SCRIPT})。")
        time.sleep(5)
        sys.exit(1)
        
    print("[+] 正在启动本地主服务...")
    try:
        subprocess.run([sys.executable, MAIN_SCRIPT])
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    download_latest_code()
    start_app()