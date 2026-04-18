import os
import time
import socket
import subprocess
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify, Response

dns_bp = Blueprint("dns", __name__, url_prefix="/dns")

# ================= Cloudflare =================
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
CF_API = "https://api.cloudflare.com/client/v4"

headers = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

# ================= Host =================
# 配置所有需要管理的主机
HOSTS = {
    "one": {
        "lan_ipv4": "192.168.31.204",
        "ipv4dns": "one4.su7.dpdns.org",
        "ipv6dns": "one.su7.dpdns.org",
        "last_ipv6": None,
        "last_seen": 0
    },
    "fast": {
        "lan_ipv4": "192.168.31.82",
        "ipv4dns": "fast4.su7.dpdns.org",
        "ipv6dns": "fast.su7.dpdns.org",
        "last_ipv6": None,
        "last_seen": 0
    },
    "one2": {
        "lan_ipv4": "192.168.31.19",
        "ipv4dns": "one24.su7.dpdns.org",
        "ipv6dns": "one2.su7.dpdns.org",
        "last_ipv6": None,
        "last_seen": 0
    },
    "15xa": {
        "lan_ipv4": "192.168.31.72",
        "ipv4dns": "15xa4.su7.dpdns.org",
        "ipv6dns": "15xa.su7.dpdns.org",
        "last_ipv6": None,
        "last_seen": 0
    },
    "15xb": {
        "lan_ipv4": "192.168.31.73",
        "ipv4dns": "15xb4.su7.dpdns.org",
        "ipv6dns": "15xb.su7.dpdns.org",
        "last_ipv6": None,
        "last_seen": 0
    },
    "15x": {
        "lan_ipv4": "192.168.31.124",
        "ipv4dns": "15x4.su7.dpdns.org",
        "ipv6dns": "15x.su7.dpdns.org",
        "last_ipv6": None,
        "last_seen": 0
    },
    "apple": {
        "lan_ipv4": "192.168.31.197",
        "ipv4dns": "apple4.su7.dpdns.org",
        "ipv6dns": "apple.su7.dpdns.org",
        "last_ipv6": None,
        "last_seen": 0
    }
}

# ================= Zone =================
def get_zone_id(domain):
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        root = ".".join(parts[i:])
        r = requests.get(
            f"{CF_API}/zones",
            headers=headers,
            params={"name": root}
        ).json()
        if r.get("result"):
            return r["result"][0]["id"]
    return None

CF_ZONE_ID = get_zone_id("su7.dpdns.org")
if not CF_ZONE_ID:
    print("❌ 无法获取Cloudflare Zone ID，请检查域名配置和API权限")
    exit(1)

# ================= DNS =================
def cf_get(name, rtype="AAAA"):
    try:
        url = f"{CF_API}/zones/{CF_ZONE_ID}/dns_records?type={rtype}&name={name}"
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            result = response.json()
            if result.get('success') and result.get('result'):
                return result['result'][0]
        return None
    except Exception as e:
        print(f"❌ 获取记录 {name} 时出错: {str(e)}")
        return None

def cf_upsert(name, ip, rtype="AAAA"):
    try:
        rec = cf_get(name, rtype)
        
        data = {
            "type": rtype,
            "name": name,
            "content": ip,
            "ttl": 120,
            "proxied": False
        }
        
        if rec:
            current_content = rec.get("content")
            is_placeholder = (current_content == "192.0.2.1" or current_content == "2001:db8::1")
            
            if current_content == ip and not is_placeholder:
                print(f"ℹ️  记录 {name} 已存在且IP相同，无需更新")
                return True
            
            url = f"{CF_API}/zones/{CF_ZONE_ID}/dns_records/{rec['id']}"
            response = requests.put(url, headers=headers, json=data)
            action = "更新"
        else:
            url = f"{CF_API}/zones/{CF_ZONE_ID}/dns_records"
            response = requests.post(url, headers=headers, json=data)
            action = "创建"
        
        if response.status_code in [200, 201]:
            result = response.json()
            if result.get('success'):
                print(f"✅ {action}记录成功: {name} -> {ip}")
                return True
            else:
                errors = result.get('errors', [])
                error_msg = '; '.join([f"{e.get('code')}: {e.get('message')}" for e in errors])
                print(f"❌ {action}记录失败: {error_msg}")
                return False
        else:
            if response.status_code == 400:
                result = response.json()
                for error in result.get('errors', []):
                    if error.get('code') == 81058:
                        print(f"ℹ️  记录 {name} 已存在（Cloudflare重复记录错误），跳过创建")
                        return True
            
            print(f"❌ HTTP请求失败: 状态码 {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ 操作 {name} 时发生错误: {str(e)}")
        return False

def should_register_domain(domain, rtype, current_ip=None):
    try:
        if rtype == "A":
            try:
                resolved_ip = socket.gethostbyname(domain)
                print(f"🔍 IPv4域名 {domain} 当前解析到: {resolved_ip}")
                
                if resolved_ip in ["192.0.2.1", "192.0.2.2", "192.0.2.3"]:
                    return True
                
                if current_ip and resolved_ip != current_ip:
                    return True
                
                if ping(resolved_ip):
                    return False
                else:
                    return True
            except socket.gaierror:
                return True
                
        elif rtype == "AAAA":
            try:
                addr_info = socket.getaddrinfo(domain, None, socket.AF_INET6)
                resolved_ipv6 = addr_info[0][4][0]
                print(f"🔍 IPv6域名 {domain} 当前解析到: {resolved_ipv6}")
                
                if resolved_ipv6 in ["2001:db8::1", "2001:db8::2"]:
                    return True
                
                if ping6(domain):
                    return False
                else:
                    return True
            except (socket.gaierror, OSError):
                return True
                
        return True
    except Exception:
        return True

def get_current_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return None

def auto_register_all_domains():
    print("🚀 开始首次自动注册所有域名...")
    current_lan_ip = get_current_lan_ip()
    print(f"🏠 检测到当前局域网IP: {current_lan_ip}")
    
    placeholder_ips = {
        "A": ["192.0.2.1", "192.0.2.2", "192.0.2.3"],
        "AAAA": ["2001:db8::1", "2001:db8::2", "2001:db8::3"]
    }
    
    success_count = 0
    total_count = 0
    
    for name, host in HOSTS.items():
        print(f"\n🔧 处理主机: {name} ({host['lan_ipv4']})")
        
        total_count += 1
        current_record = cf_get(host['ipv4dns'], 'A')
        if current_record:
            current_content = current_record.get('content')
            if current_content in placeholder_ips['A'] or current_content != host['lan_ipv4']:
                if cf_upsert(host["ipv4dns"], host['lan_ipv4'], "A"):
                    success_count += 1
            else:
                success_count += 1
        else:
            if cf_upsert(host["ipv4dns"], host['lan_ipv4'], "A"):
                success_count += 1
        
        total_count += 1
        current_v6_record = cf_get(host['ipv6dns'], 'AAAA')
        if current_v6_record:
            current_v6_content = current_v6_record.get('content')
            if current_v6_content in placeholder_ips['AAAA']:
                success_count += 1
            else:
                success_count += 1
        else:
            if cf_upsert(host["ipv6dns"], "2001:db8::1", "AAAA"):
                success_count += 1
    
    print(f"\n🎉 首次注册完成! 成功: {success_count}/{total_count} 条记录")

# ================= 工具函数 =================
def find_host(lan_ip):
    for name, h in HOSTS.items():
        if h["lan_ipv4"] == lan_ip:
            return name, h
    return None, None

def ping(ip):
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            stdout=subprocess.DEVNULL
        ).returncode == 0
    except:
        return False

def ping6(host):
    try:
        return subprocess.run(
            ["ping6", "-c", "1", "-W", "1", host],
            stdout=subprocess.DEVNULL
        ).returncode == 0
    except:
        return False

def verify_single_host(name, h):
    ipv4_ok = ping(h["lan_ipv4"])
    
    try:
        resolved_ipv4 = socket.gethostbyname(h["ipv4dns"])
        dns_ok = True
        ipv4_resolved_correctly = (resolved_ipv4 == h["lan_ipv4"])
    except:
        dns_ok = False
        ipv4_resolved_correctly = False

    try:
        socket.getaddrinfo(h["ipv6dns"], None)
        ipv6_dns_ok = True
    except:
        ipv6_dns_ok = False

    return name, {
        **h,
        "ipv4_ok": ipv4_ok,
        "dns_ok": dns_ok,
        "ipv4_resolved_correctly": ipv4_resolved_correctly,
        "ipv6_dns_ok": ipv6_dns_ok
    }

# ================= API =================
@dns_bp.route("/register", methods=["POST"])
def register():
    d = request.json
    name = d.get("name")
    ipv4 = d["lan_ipv4"]
    
    if not name:
        for existing_name, host_info in HOSTS.items():
            if host_info["lan_ipv4"] == ipv4:
                name = existing_name
                break
    
    if name in HOSTS:
        host = HOSTS[name]
        
        if host["lan_ipv4"] != d["lan_ipv4"]:
            host["lan_ipv4"] = d["lan_ipv4"]
            cf_upsert(host["ipv4dns"], d["lan_ipv4"], "A")
        
        if "ipv6" in d:
            current_ipv6_record = cf_get(host["ipv6dns"], "AAAA")
            if current_ipv6_record:
                current_content = current_ipv6_record.get("content")
                if current_content != d["ipv6"]:
                    cf_upsert(host["ipv6dns"], d["ipv6"], "AAAA")
                    host["last_ipv6"] = d["ipv6"]
            else:
                cf_upsert(host["ipv6dns"], d["ipv6"], "AAAA")
                host["last_ipv6"] = d["ipv6"]
        
        host["last_seen"] = time.time()
        return jsonify({"status": "updated"})
    
    HOSTS[name] = {
        "lan_ipv4": d["lan_ipv4"],
        "ipv4dns": d["ipv4_domain"],
        "ipv6dns": d["ipv6_domain"],
        "last_ipv6": d.get("ipv6"),
        "last_seen": time.time()
    }
    
    cf_upsert(d["ipv4_domain"], d["lan_ipv4"], "A")
    if "ipv6" in d:
        cf_upsert(d["ipv6_domain"], d["ipv6"], "AAAA")

    return jsonify({"status": "registered"})

@dns_bp.route("/update", methods=["POST"])
def update():
    ipv6 = request.json.get("ipv6")
    lan_ip = request.remote_addr

    name, host = find_host(lan_ip)
    if not host:
        return jsonify({"error": "unknown"}), 403

    if ipv6 == host["last_ipv6"]:
        host["last_seen"] = time.time()
        return jsonify({"status": "no change"})

    cf_upsert(host["ipv6dns"], ipv6, "AAAA")

    host["last_ipv6"] = ipv6
    host["last_seen"] = time.time()

    return jsonify({"status": "updated"})

@dns_bp.route("/manual_update", methods=["POST"])
def manual_update():
    d = request.json
    name, host = find_host(d["lan_ipv4"])

    if not host:
        return jsonify({"error": "not found"}), 404

    cf_upsert(host["ipv4dns"], host["lan_ipv4"], "A")
    if "ipv6" in d:
        cf_upsert(host["ipv6dns"], d["ipv6"], "AAAA")
        host["last_ipv6"] = d["ipv6"]

    host["last_seen"] = time.time()

    return jsonify({"status": "ok"})

@dns_bp.route("/api/status")
def api_status():
    result = {}
    
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = [executor.submit(verify_single_host, name, h) for name, h in HOSTS.items()]
        
        for future in futures:
            name, data = future.result()
            result[name] = data

    return jsonify(result)

# ================= 初始化 =================
def init_dns_service():
    """初始化DNS服务"""
    if CF_ZONE_ID:
        print(f"✅ Cloudflare Zone ID 获取成功: {CF_ZONE_ID[:8]}...")
        auto_register_all_domains()
    else:
        print("❌ 无法获取Cloudflare Zone ID，请检查配置")

# init_dns_service()