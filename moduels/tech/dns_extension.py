"""DNS 管理 Blueprint，兼容既有 /dns API。

依赖: Flask, requests
可选环境变量: CF_API_TOKEN, CF_ZONE_ID, DNS_DOMAIN_SUFFIX,
               DNS_STATE_FILE, DNS_PANEL_API_TOKEN, DNS_STATUS_TTL
"""

import copy
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from flask import Blueprint, jsonify, render_template, request


dns_bp = Blueprint("dns", __name__, url_prefix="/dns", template_folder="templates")
logger = logging.getLogger(__name__)

CF_API = "https://api.cloudflare.com/client/v4"
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "").strip()
DOMAIN_SUFFIX = os.getenv("DNS_DOMAIN_SUFFIX", "su7.dpdns.org").strip().lower().rstrip(".")
PANEL_TOKEN = os.getenv("DNS_PANEL_API_TOKEN", "")
# 相对路径按 dns.py 所在目录解析，避免随 Flask 启动目录变化。
_STATE_CONFIG = os.getenv("DNS_STATE_FILE", "db/dns_state.json").strip()
STATE_FILE = Path(_STATE_CONFIG).expanduser()
if not STATE_FILE.is_absolute():
    STATE_FILE = Path(__file__).resolve().parent / STATE_FILE
STATE_FILE = STATE_FILE.resolve()
# 非 ICMP 服务检测：仅端口真正建立 TCP 连接算可达。
IPV6_CHECK_PORTS = tuple(int(x) for x in os.getenv("DNS_IPV6_CHECK_PORTS", "22,80,443").split(",") if x.strip().isdigit() and 1 <= int(x) <= 65535)
STATUS_TTL = max(0, int(os.getenv("DNS_STATUS_TTL", "15")))

_ZONE_ID = os.getenv("CF_ZONE_ID", "").strip() or None
_ZONE_LOCK = threading.Lock()
_DATA_LOCK = threading.RLock()
_UPDATE_LOCK = threading.RLock()
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE = {"time": 0.0, "revision": -1, "data": None}
_DATA_REVISION = 0

# 保持原有主机配置及域名命名规则。
_SEED_LANS = {
    "one": "192.168.31.204",
    "fast": "192.168.31.82",
    "one2": "192.168.31.18",
    "15xa": "192.168.31.241",
    "15xb": "192.168.31.243",
    "15x": "192.168.31.240",
    "apple": "192.168.31.197",
}


HOSTS = {
    name: {
        "lan_ipv4": lan,
        "ipv4dns": f"{name}4.{DOMAIN_SUFFIX}",
        "ipv6dns": f"{name}.{DOMAIN_SUFFIX}",
        "last_ipv6": None,
        "last_seen": 0,
    }
    for name, lan in _SEED_LANS.items()
}

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAC_RE = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2}(?![0-9a-f])", re.I)


class DNSServiceError(Exception):
    """可返回给前端的可控业务错误。"""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def _private_ipv4(value):
    try:
        ip = ipaddress.IPv4Address(value)
        rfc1918 = (ipaddress.IPv4Network("10.0.0.0/8"),
                   ipaddress.IPv4Network("172.16.0.0/12"),
                   ipaddress.IPv4Network("192.168.0.0/16"))
        if not any(ip in network for network in rfc1918):
            raise ValueError("not an RFC1918 LAN address")
        return str(ip)
    except (ValueError, ipaddress.AddressValueError, TypeError):
        raise DNSServiceError("lan_ipv4 必须是有效的局域网 IPv4", 400)


def _public_ipv6(value):
    try:
        ip = ipaddress.IPv6Address(value)
        if not ip.is_global or ip.ipv4_mapped is not None:
            raise ValueError("not globally routable")
        return str(ip)
    except (ValueError, TypeError):
        raise DNSServiceError("IPv6 必须是有效的全球单播地址（不接受 fe80、fd、2001:db8 等地址）", 400)


def _domain(value):
    value = str(value or "").strip().lower().rstrip(".")
    if not value.endswith("." + DOMAIN_SUFFIX):
        raise DNSServiceError(f"域名必须属于 {DOMAIN_SUFFIX} 的子域名", 400)
    labels = value.split(".")
    if len(value) > 253 or any(not _NAME_RE.fullmatch(label) for label in labels):
        raise DNSServiceError("域名格式不合法", 400)
    return value


def _hostname(value):
    name = str(value or "").strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise DNSServiceError("主机名只能包含小写字母、数字、连字符，长度最多 63", 400)
    return name


def _load_state():
    if not STATE_FILE.exists():
        return
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state is not an object")
        for name, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                name = _hostname(name)
                ip6 = item.get("last_ipv6")
                ip6 = _public_ipv6(ip6) if ip6 else None
                seen = max(float(item.get("last_seen") or 0), 0)
                if name in _SEED_LANS:
                    # 固定主机的 IPv4 / 域名以源代码为准，文件仅保存动态状态。
                    HOSTS[name]["last_ipv6"] = ip6
                    HOSTS[name]["last_seen"] = seen
                else:
                    HOSTS[name] = {
                        "lan_ipv4": _private_ipv4(item.get("lan_ipv4")),
                        "ipv4dns": _domain(item.get("ipv4dns")),
                        "ipv6dns": _domain(item.get("ipv6dns")),
                        "last_ipv6": ip6,
                        "last_seen": seen,
                    }
            except (DNSServiceError, ValueError, TypeError):
                logger.warning("跳过无效的 DNS 状态条目: %r", name)
    except (OSError, ValueError) as exc:
        logger.error("读取 DNS 状态文件失败: %s", exc)


def _write_state_locked():
    """调用方持有 _DATA_LOCK；原子替换，防止进程中断时写坏 JSON。"""
    tmp_name = None
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=STATE_FILE.parent,
            prefix=".dns_state_", suffix=".tmp", delete=False,
        ) as file:
            tmp_name = file.name
            os.chmod(tmp_name, 0o600)
            json.dump(HOSTS, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_name, STATE_FILE)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _commit_host(name, value):
    global _DATA_REVISION
    with _DATA_LOCK:
        old_value = copy.deepcopy(HOSTS.get(name))
        HOSTS[name] = copy.deepcopy(value)
        try:
            _write_state_locked()
        except OSError as exc:
            if old_value is None:
                HOSTS.pop(name, None)
            else:
                HOSTS[name] = old_value
            logger.exception("DNS 状态持久化失败")
            raise DNSServiceError("Cloudflare 可能已更新，但本地状态文件保存失败，请检查 DNS_STATE_FILE 权限", 500) from exc
        _DATA_REVISION += 1


def _snapshot():
    with _DATA_LOCK:
        return copy.deepcopy(HOSTS), _DATA_REVISION


def find_host(lan_ip):
    with _DATA_LOCK:
        for name, host in HOSTS.items():
            if host["lan_ipv4"] == lan_ip:
                return name, copy.deepcopy(host)
    return None, None


def _host_by_name(name):
    with _DATA_LOCK:
        host = HOSTS.get(name)
        return copy.deepcopy(host) if host else None


def _cf_request(method, endpoint, **kwargs):
    if not CF_API_TOKEN:
        raise DNSServiceError("未配置 CF_API_TOKEN", 503)
    try:
        response = requests.request(
            method, f"{CF_API}{endpoint}",
            headers={"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"},
            timeout=(3, 7), **kwargs,
        )
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Cloudflare 请求异常: %s", exc)
        raise DNSServiceError("Cloudflare 请求失败，请检查网络和 API 配置") from exc
    if not response.ok or not body.get("success", False):
        details = "; ".join(str(err.get("message", "unknown")) for err in body.get("errors", []))
        logger.error("Cloudflare API 错误 HTTP %s: %s", response.status_code, details)
        raise DNSServiceError(f"Cloudflare API 操作失败：{details or response.status_code}")
    return body


def get_zone_id():
    global _ZONE_ID
    if _ZONE_ID:
        return _ZONE_ID
    with _ZONE_LOCK:
        if _ZONE_ID:
            return _ZONE_ID
        parts = DOMAIN_SUFFIX.split(".")
        for i in range(len(parts) - 1):
            zone_name = ".".join(parts[i:])
            result = _cf_request("GET", "/zones", params={"name": zone_name, "per_page": 1})
            if result.get("result"):
                _ZONE_ID = result["result"][0]["id"]
                return _ZONE_ID
        raise DNSServiceError("找不到对应 Cloudflare Zone，请配置 CF_ZONE_ID 或检查域名权限", 503)


def cf_get(name, rtype="AAAA"):
    _domain(name)
    if rtype not in ("A", "AAAA"):
        raise DNSServiceError("不支持的 DNS 记录类型", 400)
    zone_id = get_zone_id()
    body = _cf_request("GET", f"/zones/{zone_id}/dns_records",
                       params={"type": rtype, "name": name, "per_page": 100})
    records = body.get("result") or []
    if len(records) > 1:
        logger.warning("%s 存在多条 %s 记录；仅管理第一条", name, rtype)
    return records[0] if records else None


def cf_upsert(name, ip, rtype="AAAA"):
    """成功或无需更新时返回 True，失败抛出异常；不把 API 故障当成记录不存在。"""
    if rtype == "A":
        ip = str(ipaddress.IPv4Address(ip))
    elif rtype == "AAAA":
        ip = _public_ipv6(ip)
    else:
        raise DNSServiceError("不支持的 DNS 记录类型", 400)
    record = cf_get(name, rtype)
    if record and record.get("content") == ip and record.get("proxied") is False:
        return True
    payload = {"type": rtype, "name": name, "content": ip, "ttl": 120, "proxied": False}
    zone_id = get_zone_id()
    if record:
        _cf_request("PUT", f"/zones/{zone_id}/dns_records/{record['id']}", json=payload)
    else:
        _cf_request("POST", f"/zones/{zone_id}/dns_records", json=payload)
    logger.info("Cloudflare %s %s -> %s", rtype, name, ip)
    return True


def _run(command, timeout=3):
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ping(ip):
    cmd = ["ping", "-c", "1", "-W", "1", ip]
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=3, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ping6(ip):
    # Linux 优先 ping -6；macOS/BSD 回退 ping6。
    for cmd in (["ping", "-6", "-c", "1", "-W", "1", ip],
                ["ping6", "-c", "1", "-W", "1", ip]):
        try:
            if subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=3, check=False).returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def _resolve(name, family):
    try:
        rows = socket.getaddrinfo(name, None, family, socket.SOCK_STREAM)
        return sorted({str(ipaddress.ip_address(row[4][0])) for row in rows})
    except (OSError, ValueError):
        return []


def ipv6_reachable(address):
    """IPv6 可以 ICMP ping 通，或者指定 TCP 服务可连接，则认为可达。"""
    if not address:
        return None
    if ping6(address):
        return True
    for port in IPV6_CHECK_PORTS:
        try:
            with socket.create_connection((address, port), timeout=0.8):
                return True
        except (OSError, TimeoutError):
            continue
    return False


def verify_single_host(name, host):
    lan = host["lan_ipv4"]
    v4 = _resolve(host["ipv4dns"], socket.AF_INET)
    v6 = _resolve(host["ipv6dns"], socket.AF_INET6)
    expected_v6 = host.get("last_ipv6")
    # 必须有当前 IPv6，且 DNS 实际解析包含该地址才是绿灯。
    # ipaddress 规范化地址，可识别不同的 IPv6 缩写形式。
    desired = ipaddress.IPv6Address(expected_v6) if expected_v6 else None
    v6_dns_ok = (any(ipaddress.IPv6Address(value) == desired for value in v6)
                 if desired else None)
    return name, {
        **host,
        "ipv4_ok": ping(lan),
        "dns_ok": lan in v4,
        "ipv4_resolved_correctly": lan in v4,
        "ipv6_dns_ok": v6_dns_ok,
        "ipv6_ok": ipv6_reachable(expected_v6),
        "ipv4_resolved": v4,
        "ipv6_resolved": v6,
    }


def _normalize_mac(mac):
    return ":".join(f"{int(piece, 16):02x}" for piece in mac.split(":"))


def _discover_mac(ipv4):
    # Linux iproute2；随后兼容 macOS/BSD 与部分 Linux 的 arp。
    for command in (["ip", "neigh", "show", ipv4], ["arp", "-n", ipv4],
                    ["arp", "-a", ipv4]):
        found = _MAC_RE.search(_run(command))
        if found:
            return _normalize_mac(found.group(0))
    return None


def get_ipv6_from_lan_ipv4(ipv4):
    """通过 IPv4 的邻居 MAC 匹配 IPv6 邻居表；无法发现则返回 None。"""
    mac = _discover_mac(ipv4)
    if not mac:
        ping(ipv4)  # 尝试触发 ARP/邻居发现
        mac = _discover_mac(ipv4)
    if not mac:
        return None
    for command in (["ip", "-6", "neigh", "show"], ["ndp", "-an"], ["ndp", "-a"]):
        for line in _run(command).splitlines():
            if not any(_normalize_mac(match.group(0)) == mac
                       for match in _MAC_RE.finditer(line)):
                continue
            candidate = line.split()[0].split("%")[0]
            try:
                return _public_ipv6(candidate)
            except DNSServiceError:
                continue
    return None


def _update_host_ipv6(name, ipv6, update_a=False):
    ipv6 = _public_ipv6(ipv6)
    with _UPDATE_LOCK:
        host = _host_by_name(name)
        if not host:
            raise DNSServiceError("主机不存在", 404)
        if update_a:
            cf_upsert(host["ipv4dns"], host["lan_ipv4"], "A")
        cf_upsert(host["ipv6dns"], ipv6, "AAAA")
        host["last_ipv6"] = ipv6
        host["last_seen"] = time.time()
        _commit_host(name, host)
    return {"status": "updated", "ipv6": ipv6}


def _json_body():
    if not request.is_json:
        raise DNSServiceError("请求必须是 JSON", 400)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise DNSServiceError("JSON 格式不正确", 400)
    return body


@dns_bp.before_request
def _protect_writes():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE") or not PANEL_TOKEN:
        return None
    provided = request.headers.get("X-DNS-Panel-Token", "")
    auth = request.headers.get("Authorization", "")
    if not provided and auth.lower().startswith("bearer "):
        provided = auth[7:]
    if not hmac.compare_digest(provided, PANEL_TOKEN):
        return jsonify({"error": "操作需要有效的 DNS_PANEL_API_TOKEN"}), 401
    return None


@dns_bp.errorhandler(DNSServiceError)
def _handle_dns_error(error):
    return jsonify({"error": str(error)}), error.status


@dns_bp.get("/")
def panel():
    return render_template("dns/index.html")


@dns_bp.get("/api/hosts")
def api_hosts():
    hosts, _ = _snapshot()
    response = jsonify(hosts)
    response.headers["Cache-Control"] = "no-store"
    return response


@dns_bp.get("/api/status")
def api_status():
    hosts, revision = _snapshot()
    now = time.monotonic()
    with _STATUS_LOCK:
        cached = _STATUS_CACHE
        if (cached["data"] is not None and cached["revision"] == revision
                and now - cached["time"] < STATUS_TTL):
            result = copy.deepcopy(cached["data"])
        else:
            result = None

    if result is None:
        result = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(hosts)))) as executor:
            tasks = {executor.submit(verify_single_host, n, h): (n, h)
                     for n, h in hosts.items()}
            for future in as_completed(tasks):
                name, host = tasks[future]
                try:
                    _, data = future.result()
                except Exception:
                    logger.exception("检测主机状态失败: %s", name)
                    data = {**host, "ipv4_ok": None, "dns_ok": None,
                            "ipv4_resolved_correctly": None, "ipv6_dns_ok": None,
                            "ipv6_ok": None, "ipv4_resolved": [], "ipv6_resolved": []}
                result[name] = data
        with _DATA_LOCK:
            latest_revision = _DATA_REVISION
        if latest_revision == revision:
            with _STATUS_LOCK:
                _STATUS_CACHE.update(time=time.monotonic(), revision=revision,
                                     data=copy.deepcopy(result))
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@dns_bp.post("/register")
def register():
    body = _json_body()
    lan = _private_ipv4(body.get("lan_ipv4"))
    raw_name = body.get("name")
    if raw_name:
        name = _hostname(raw_name)
    else:
        name, _ = find_host(lan)
        if not name:
            raise DNSServiceError("未知 IP，请同时提供 name", 400)

    ipv6 = _public_ipv6(body["ipv6"]) if body.get("ipv6") else None
    with _UPDATE_LOCK:
        existing = _host_by_name(name)
        host = existing or {
            "lan_ipv4": lan,
            "ipv4dns": _domain(body.get("ipv4_domain", f"{name}4.{DOMAIN_SUFFIX}")),
            "ipv6dns": _domain(body.get("ipv6_domain", f"{name}.{DOMAIN_SUFFIX}")),
            "last_ipv6": None, "last_seen": 0,
        }
        # 已注册主机的域名由现有配置控制；不允许 register 静默改名。
        cf_upsert(host["ipv4dns"], lan, "A")
        host["lan_ipv4"] = lan
        if ipv6:
            cf_upsert(host["ipv6dns"], ipv6, "AAAA")
            host["last_ipv6"] = ipv6
        host["last_seen"] = time.time()
        _commit_host(name, host)
    return jsonify({"status": "updated" if existing else "registered", "name": name})


@dns_bp.post("/update")
def update():
    """兼容设备自报：从 request.remote_addr 匹配 LAN IPv4。"""
    body = _json_body()
    name, host = find_host(request.remote_addr)
    if not host:
        raise DNSServiceError("请求 IP 不属于已知主机", 403)
    ipv6 = _public_ipv6(body["ipv6"]) if body.get("ipv6") else None
    if ipv6 and ipv6 != host.get("last_ipv6"):
        return jsonify(_update_host_ipv6(name, ipv6))
    with _UPDATE_LOCK:
        host = _host_by_name(name)
        host["last_seen"] = time.time()
        _commit_host(name, host)
    return jsonify({"status": "no change", "ipv6": host.get("last_ipv6")})


@dns_bp.post("/manual_update")
def manual_update():
    body = _json_body()
    name, host = find_host(_private_ipv4(body.get("lan_ipv4")))
    if not host:
        raise DNSServiceError("找不到对应主机", 404)
    if "ipv6" in body:
        result = _update_host_ipv6(name, body["ipv6"], update_a=True)
        return jsonify({"status": "ok", "ipv6": result["ipv6"]})
    with _UPDATE_LOCK:
        current = _host_by_name(name)
        cf_upsert(current["ipv4dns"], current["lan_ipv4"], "A")
        current["last_seen"] = time.time()
        _commit_host(name, current)
    return jsonify({"status": "ok"})


@dns_bp.post("/auto_ipv6_update")
def auto_ipv6_update():
    body = _json_body()
    name, host = find_host(_private_ipv4(body.get("lan_ipv4")))
    if not host:
        raise DNSServiceError("找不到对应主机", 404)
    ipv6 = get_ipv6_from_lan_ipv4(host["lan_ipv4"])
    if not ipv6:
        raise DNSServiceError("无法通过邻居表发现公网 IPv6，请确认主机在线且与服务端位于可发现的局域网", 404)
    return jsonify(_update_host_ipv6(name, ipv6))


def _auto_update_one(name, host):
    try:
        ipv6 = get_ipv6_from_lan_ipv4(host["lan_ipv4"])
        if not ipv6:
            return name, {"status": "not_found", "error": "邻居表未发现公网 IPv6"}
        return name, _update_host_ipv6(name, ipv6)
    except DNSServiceError as exc:
        return name, {"status": "failed", "error": str(exc)}
    except Exception:
        logger.exception("自动更新失败: %s", name)
        return name, {"status": "error", "error": "内部错误"}


@dns_bp.post("/auto_ipv6_update_all")
def auto_ipv6_update_all():
    hosts, _ = _snapshot()
    if not hosts:
        return jsonify({})
    result = {}
    with ThreadPoolExecutor(max_workers=min(6, len(hosts))) as executor:
        futures = [executor.submit(_auto_update_one, name, host)
                   for name, host in hosts.items()]
        for future in as_completed(futures):
            name, data = future.result()
            result[name] = data
    return jsonify(result)


def init_dns_service():
    """可选初始化；不会自动在模块 import 时访问 Cloudflare。"""
    hosts, _ = _snapshot()
    successes = 0
    for name, host in hosts.items():
        try:
            cf_upsert(host["ipv4dns"], host["lan_ipv4"], "A")
            if host.get("last_ipv6"):
                cf_upsert(host["ipv6dns"], host["last_ipv6"], "AAAA")
            # 不再创建 2001:db8::1 等无效占位 AAAA 记录。
            successes += 1
        except DNSServiceError as exc:
            logger.error("初始化 %s 失败: %s", name, exc)
    logger.info("DNS 初始化完成: %s/%s 台主机", successes, len(hosts))
    return successes == len(hosts)


def auto_register_all_domains():
    """向后兼容旧代码调用。"""
    return init_dns_service()


def get_current_lan_ip():
    """保留旧代码中的辅助函数，不会发送网络数据包。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


_load_state()
if not PANEL_TOKEN:
    logger.warning("DNS_PANEL_API_TOKEN 未设置：/dns 的写入接口未启用额外鉴权；请限制访问范围")
