import socket
import requests
import subprocess

def get_ipv4():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip

def get_ipv6():
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip if ip.startswith(('2', '3')) else None
    except:
        return None

def upload_ips(server_url ):
    ipv4 = get_ipv4()
    ipv6 = get_ipv6()
    
    payload = {
        
        "lan_ipv4": ipv4,
        "last_ipv6": ipv6,
         
    }
    print(f"Detected IPv4  : {ipv4}, IPv6: {ipv6}")
    if ipv6:
        payload["ipv6"] = ipv6
    
    response = requests.post(f"{server_url}/dns/register", json=payload)
    print(f"Upload result: {response.json()}")

# 使用示例
upload_ips("http://apple4.su7.dpdns.org:8100" )