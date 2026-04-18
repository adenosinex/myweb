import time
import socket
import requests

def get_ipv4():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip 
    except Exception:
        return None

def get_ipv6():
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip.startswith(('2', '3')) else None
    except Exception:
        return None

def upload_ips(server_url, ipv4, ipv6):
    if not ipv4:
        print("⚠️ 无法获取本地 IPv4，中止本次上报")
        return

    payload = {
        "lan_ipv4": ipv4,
    }
    if ipv6:
        payload["ipv6"] = ipv6
        payload["last_ipv6"] = ipv6  # 保留你原代码的结构以防后端需要

    print(f"📤 正在上报 -> IPv4: {ipv4}, IPv6: {ipv6}")
    
    try:
        # 增加 timeout 防止网络阻塞导致脚本卡死
        response = requests.post(f"{server_url}/dns/register", json=payload, timeout=10)
        print(f"✅ 上报结果: {response.json()}")
    except Exception as e:
        print(f"❌ 上报失败: {e}")

def monitor_and_update(server_url, check_interval=300):
    print("🚀 启动 DNS 客户端监控...")
    
    # 1. 启动时执行一次
    current_ipv4 = get_ipv4()
    last_ipv6 = get_ipv6()
    upload_ips(server_url, current_ipv4, last_ipv6)

    # 2. 循环定时检查
    while True:
        time.sleep(check_interval)
        
        current_ipv6 = get_ipv6()
        
        # 判断 IPv6 是否发生变化
        if current_ipv6 != last_ipv6:
            print(f"\n🔄 检测到 IPv6 发生变化: {last_ipv6} -> {current_ipv6}")
            current_ipv4 = get_ipv4()  # IP发生变化时，顺便重新获取最新 IPv4
            
            upload_ips(server_url, current_ipv4, current_ipv6)
            last_ipv6 = current_ipv6

if __name__ == "__main__":
    # 配置区
    SERVER_URL = "http://apple4.su7.dpdns.org:8100"
    CHECK_INTERVAL = 300  # 检查间隔时间（秒），默认 5 分钟
    
    monitor_and_update(SERVER_URL, CHECK_INTERVAL)