import os
import time
import socket
import subprocess
import requests
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
            # 检查IP是否相同（排除占位符IP）
            current_content = rec.get("content")
            
            # 如果当前是占位符IP，则需要更新
            is_placeholder = (current_content == "192.0.2.1" or current_content == "2001:db8::1")
            
            if current_content == ip and not is_placeholder:
                print(f"ℹ️  记录 {name} 已存在且IP相同，无需更新")
                return True
            
            # 更新现有记录
            url = f"{CF_API}/zones/{CF_ZONE_ID}/dns_records/{rec['id']}"
            response = requests.put(url, headers=headers, json=data)
            action = "更新"
        else:
            # 创建新记录
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
                print(f"   响应: {result}")
                return False
        else:
            # 特殊处理Cloudflare的重复记录错误
            if response.status_code == 400:
                result = response.json()
                for error in result.get('errors', []):
                    if error.get('code') == 81058:  # 重复记录错误
                        print(f"ℹ️  记录 {name} 已存在（Cloudflare重复记录错误），跳过创建")
                        return True
            
            print(f"❌ HTTP请求失败: 状态码 {response.status_code}")
            print(f"   响应内容: {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ 操作 {name} 时发生错误: {str(e)}")
        return False

# ================= 改进的域名检查函数 =================
def should_register_domain(domain, rtype, current_ip=None):
    """
    检查是否需要注册域名
    current_ip: 当前实际IP，如果提供则直接比较
    """
    try:
        if rtype == "A":  # IPv4
            try:
                resolved_ip = socket.gethostbyname(domain)
                print(f"🔍 IPv4域名 {domain} 当前解析到: {resolved_ip}")
                
                # 检查是否是占位符IP
                if resolved_ip in ["192.0.2.1", "192.0.2.2", "192.0.2.3"]:
                    print(f"⚠️  域名 {domain} 解析到占位符IP，需要更新")
                    return True
                
                # 如果提供了当前IP，直接比较
                if current_ip and resolved_ip != current_ip:
                    print(f"⚠️  域名 {domain} IP不匹配，需要更新: {resolved_ip} -> {current_ip}")
                    return True
                
                # 检查是否可以ping通
                if ping(resolved_ip):
                    print(f"✅ IPv4域名 {domain} 可访问，跳过注册")
                    return False
                else:
                    print(f"⚠️  IPv4域名 {domain} 解析成功但无法ping通，需要更新")
                    return True
            except socket.gaierror:
                print(f"ℹ️  IPv4域名 {domain} 无法解析，需要创建")
                return True
                
        elif rtype == "AAAA":  # IPv6
            try:
                addr_info = socket.getaddrinfo(domain, None, socket.AF_INET6)
                resolved_ipv6 = addr_info[0][4][0]
                print(f"🔍 IPv6域名 {domain} 当前解析到: {resolved_ipv6}")
                
                # 检查是否是占位符IP
                if resolved_ipv6 in ["2001:db8::1", "2001:db8::2"]:
                    print(f"⚠️  域名 {domain} 解析到占位符IPv6，需要更新")
                    return True
                
                # 检查是否可以ping通
                if ping6(domain):
                    print(f"✅ IPv6域名 {domain} 可访问，跳过注册")
                    return False
                else:
                    print(f"⚠️  IPv6域名 {domain} 解析成功但无法ping通，需要更新")
                    return True
            except (socket.gaierror, OSError) as e:
                print(f"ℹ️  IPv6域名 {domain} 无法解析: {str(e)}，需要创建")
                return True
                
        return True
    except Exception as e:
        print(f"⚠️  检查域名 {domain} 时出错: {str(e)}")
        return True

# ================= 获取当前局域网IP =================
def get_current_lan_ip():
    """获取当前机器的实际局域网IP"""
    try:
        # 通过连接外部服务器获取本机出口IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return None

# ================= 改进的自动注册函数 =================
def auto_register_all_domains():
    """
    应用启动时自动注册所有配置的域名
    只更新占位符IP或不存在的记录
    """
    print("🚀 开始首次自动注册所有域名...")
    
    # 获取当前机器的实际IP
    current_lan_ip = get_current_lan_ip()
    print(f"🏠 检测到当前局域网IP: {current_lan_ip}")
    
    # 占位符IP列表
    placeholder_ips = {
        "A": ["192.0.2.1", "192.0.2.2", "192.0.2.3"],
        "AAAA": ["2001:db8::1", "2001:db8::2", "2001:db8::3"]
    }
    
    success_count = 0
    total_count = 0
    
    for name, host in HOSTS.items():
        print(f"\n🔧 处理主机: {name} ({host['lan_ipv4']})")
        
        # 检查并更新IPv4记录
        total_count += 1
        print(f"  🌐 检查IPv4域名: {host['ipv4dns']}")
        
        # 获取当前记录
        current_record = cf_get(host['ipv4dns'], 'A')
        if current_record:
            current_content = current_record.get('content')
            print(f"    当前记录: {current_content}")
            
            # 如果当前是占位符IP，或者IP不匹配，则更新
            if current_content in placeholder_ips['A'] or current_content != host['lan_ipv4']:
                print(f"    需要更新: {current_content} -> {host['lan_ipv4']}")
                if cf_upsert(host["ipv4dns"], host['lan_ipv4'], "A"):
                    success_count += 1
                else:
                    print(f"    ❌ 更新失败")
            else:
                print(f"    ✅ 已是正确IP，无需更新")
                success_count += 1  # 已正确，视为成功
        else:
            # 记录不存在，创建新的
            print(f"    创建新记录: {host['ipv4dns']} -> {host['lan_ipv4']}")
            if cf_upsert(host["ipv4dns"], host['lan_ipv4'], "A"):
                success_count += 1
            else:
                print(f"    ❌ 创建失败")
        
        # 检查并更新IPv6记录（如果有IPv6地址的话）
        total_count += 1
        print(f"  🌐 检查IPv6域名: {host['ipv6dns']}")
        
        # 对于IPv6，暂时保持占位符或使用实际IPv6地址
        current_v6_record = cf_get(host['ipv6dns'], 'AAAA')
        if current_v6_record:
            current_v6_content = current_v6_record.get('content')
            print(f"    当前IPv6记录: {current_v6_content}")
            
            if current_v6_content in placeholder_ips['AAAA']:
                # 如果是占位符，可以更新为实际IPv6或保持占位符
                print(f"    IPv6记录是占位符，保持现状")
                success_count += 1  # 占位符也是有效的
            else:
                print(f"    ✅ IPv6记录已存在，无需更新")
                success_count += 1
        else:
            # 创建IPv6占位符记录
            print(f"    创建IPv6占位符记录: {host['ipv6dns']} -> 2001:db8::1")
            if cf_upsert(host["ipv6dns"], "2001:db8::1", "AAAA"):
                success_count += 1
            else:
                print(f"    ❌ 创建IPv6记录失败")
    
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

# ================= API =================
@dns_bp.route("/register", methods=["POST"])
def register():
    d = request.json
    name = d.get("name")  # 可选参数
    ipv4 = d["lan_ipv4"]
    
    # 如果没有提供name，根据IPv4查找已有的主机
    if not name:
        for existing_name, host_info in HOSTS.items():
            if host_info["lan_ipv4"] == ipv4:
                name = existing_name
                break
    
    # 检查是否已存在
    if name in HOSTS:
        host = HOSTS[name]
        
        # 更新IPv4（如果不同）
        if host["lan_ipv4"] != d["lan_ipv4"]:
            host["lan_ipv4"] = d["lan_ipv4"]
            # 更新IPv4 DNS记录
            cf_upsert(host["ipv4dns"], d["lan_ipv4"], "A")
        
        # 检查并更新IPv6（如果提供且与当前记录不同）
        if "ipv6" in d:
            current_ipv6_record = cf_get(host["ipv6dns"], "AAAA")
            if current_ipv6_record:
                current_content = current_ipv6_record.get("content")
                # 如果当前IPv6记录与上传的不同，则更新
                if current_content != d["ipv6"]:
                    print(f"🔄 更新IPv6记录: {host['ipv6dns']} {current_content} -> {d['ipv6']}")
                    cf_upsert(host["ipv6dns"], d["ipv6"], "AAAA")
                    host["last_ipv6"] = d["ipv6"]
            else:
                # 如果记录不存在，创建新的
                print(f"📝 创建IPv6记录: {host['ipv6dns']} -> {d['ipv6']}")
                cf_upsert(host["ipv6dns"], d["ipv6"], "AAAA")
                host["last_ipv6"] = d["ipv6"]
        
        host["last_seen"] = time.time()
        return jsonify({"status": "updated"})
    
    # 新增主机
    HOSTS[name] = {
        "lan_ipv4": d["lan_ipv4"],
        "ipv4dns": d["ipv4_domain"],
        "ipv6dns": d["ipv6_domain"],
        "last_ipv6": d.get("ipv6"),  # 如果提供了IPv6
        "last_seen": time.time()
    }
    
    # 创建DNS记录
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

    # 只更新IPv6，IPv4由实际LAN IP决定
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

    # 更新IPv4记录为实际IP
    cf_upsert(host["ipv4dns"], host["lan_ipv4"], "A")
    # 更新IPv6记录
    if "ipv6" in d:
        cf_upsert(host["ipv6dns"], d["ipv6"], "AAAA")
        host["last_ipv6"] = d["ipv6"]

    host["last_seen"] = time.time()

    return jsonify({"status": "ok"})

@dns_bp.route("/api/status")
def api_status():
    result = {}

    for name, h in HOSTS.items():
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

        result[name] = {
            **h,
            "ipv4_ok": ipv4_ok,
            "dns_ok": dns_ok,
            "ipv4_resolved_correctly": ipv4_resolved_correctly,
            "ipv6_dns_ok": ipv6_dns_ok
        }

    return jsonify(result)

# ================= 初始化 =================
def init_dns_service():
    """初始化DNS服务"""
    if CF_ZONE_ID:
        print(f"✅ Cloudflare Zone ID 获取成功: {CF_ZONE_ID[:8]}...")
        auto_register_all_domains()
    else:
        print("❌ 无法获取Cloudflare Zone ID，请检查配置")

# 在应用启动时调用
# init_dns_service()