import subprocess
import json
import re
from flask import Blueprint, request, jsonify

# =========================
# Disk Health Blueprint
# =========================
disk_bp = Blueprint('disk_bp', __name__, url_prefix='/disk')


# -------------------------
# 工具函数：执行shell命令
# -------------------------
def run_cmd(cmd: list) -> str:
    """
    执行系统命令并返回stdout
    """
    return subprocess.getoutput(" ".join(cmd))


# -------------------------
# 工具函数：解析百分比
# -------------------------
def parse_percent(text: str) -> int:
    """
    从字符串中提取百分比数值
    """
    m = re.search(r'(\d+)%', text)
    return int(m.group(1)) if m else -1


# =========================
# 1. 基础磁盘信息
# =========================
@disk_bp.route('/info', methods=['GET'])
def disk_info():
    """
    返回磁盘基础信息（容量 / 型号 / TRIM / SMART状态）
    """

    raw = run_cmd(["system_profiler", "SPNVMeDataType"])

    data = {
        "model": None,              # 磁盘型号
        "capacity_gb": None,       # 容量（GB）
        "trim_support": None,      # 是否支持TRIM
        "smart_status": None,     # SMART状态
        "firmware": None,         # 固件版本
    }

    # 解析字段
    def get(pattern):
        m = re.search(pattern, raw)
        return m.group(1).strip() if m else None

    data["model"] = get(r"Model:\s+(.*)")
    data["capacity_gb"] = get(r"Capacity:\s+([0-9.]+)\sGB")
    data["trim_support"] = "Yes" if "TRIM Support: Yes" in raw else "No"
    data["smart_status"] = get(r"S\.M\.A\.R\.T\. status:\s+(.*)")
    data["firmware"] = get(r"Revision:\s+(.*)")

    return jsonify(data)


# =========================
# 2. SMART健康数据
# =========================
@disk_bp.route('/smart', methods=['GET'])
def disk_smart():
    """
    返回NVMe SMART详细数据解析
    """

    raw = run_cmd(["smartctl", "-a", "/dev/disk0"])

    def get(pattern):
        m = re.search(pattern, raw)
        return m.group(1).strip() if m else None

    data = {
        # ===== 基础状态 =====
        "health": get(r"SMART overall-health self-assessment test result:\s+(.*)"),
        "critical_warning": get(r"Critical Warning:\s+(0x[0-9a-fA-F]+)"),

        # ===== 温度 =====
        "temperature_c": int(get(r"Temperature:\s+([0-9]+)") or -1),

        # ===== 寿命 =====
        "percentage_used": int(get(r"Percentage Used:\s+([0-9]+)") or -1),

        # ===== 容量状态 =====
        "available_spare": parse_percent(get(r"Available Spare:\s+([0-9%]+)") or ""),
        "available_spare_threshold": parse_percent(get(r"Available Spare Threshold:\s+([0-9%]+)") or ""),

        # ===== 写入统计 =====
        "data_units_read_tb": float(re.search(r"Data Units Read:\s+([0-9,]+)", raw).group(1).replace(",", "")) * 0.512 / 1024 if "Data Units Read" in raw else None,
        "data_units_written_tb": float(re.search(r"Data Units Written:\s+([0-9,]+)", raw).group(1).replace(",", "")) * 0.512 / 1024 if "Data Units Written" in raw else None,

        # ===== 稳定性 =====
        "media_errors": int(get(r"Media and Data Integrity Errors:\s+([0-9]+)") or -1),
        "error_log_entries": int(get(r"Error Information Log Entries:\s+([0-9]+)") or -1),

        # ===== 使用时间 =====
        "power_on_hours": int(get(r"Power On Hours:\s+([0-9]+)") or -1),
        "power_cycles": int(get(r"Power Cycles:\s+([0-9]+)") or -1),
        "unsafe_shutdowns": int(get(r"Unsafe Shutdowns:\s+([0-9]+)") or -1),

        # ===== 写入量原始 =====
        "data_units_read_raw": get(r"Data Units Read:\s+([0-9,]+)"),
        "data_units_written_raw": get(r"Data Units Written:\s+([0-9,]+)"),
    }

    return jsonify(data)


# =========================
# 3. 综合健康评分
# =========================
@disk_bp.route('/health', methods=['GET'])
def disk_health():
    """
    计算SSD健康评分（0-100）
    """

    raw = run_cmd(["smartctl", "-a", "/dev/disk0"])

    def get(pattern):
        m = re.search(pattern, raw)
        return m.group(1).strip() if m else None

    percentage_used = int(get(r"Percentage Used:\s+([0-9]+)") or 0)
    media_errors = int(get(r"Media and Data Integrity Errors:\s+([0-9]+)") or 0)
    critical = get(r"Critical Warning:\s+(0x[0-9a-fA-F]+)") or "0x00"

    # =========================
    # 评分模型（简单线性）
    # =========================
    score = 100

    # 寿命扣分
    score -= percentage_used * 0.8

    # 错误扣分
    score -= media_errors * 10

    # critical warning扣分
    if critical != "0x00":
        score -= 30

    score = max(0, min(100, score))

    return jsonify({
        "health_score": round(score, 2),
        "percentage_used": percentage_used,
        "media_errors": media_errors,
        "critical_warning": critical,
        "status": "GOOD" if score > 80 else "WARN" if score > 50 else "BAD"
    })


# =========================
# 4. 原始数据（调试用）
# =========================
@disk_bp.route('/raw', methods=['GET'])
def disk_raw():
    """
    返回原始 system_profiler + smartctl 输出
    """

    return jsonify({
        "system_profiler": run_cmd(["system_profiler", "SPNVMeDataType"]),
        "smartctl": run_cmd(["smartctl", "-a", "/dev/disk0"])
    })
import subprocess
import re
import math

def run(cmd):
    return subprocess.getoutput(" ".join(cmd))


def get_smart_raw():
    return run(["smartctl", "-a", "/dev/disk0"])


def parse(raw):
    def g(p):
        m = re.search(p, raw)
        return m.group(1) if m else None

    return {
        "percentage_used": float(g(r"Percentage Used:\s+([0-9]+)") or 0),
        "written_raw": float((g(r"Data Units Written:\s+([0-9,]+)") or "0").replace(",", "")),
        "media_errors": float(g(r"Media and Data Integrity Errors:\s+([0-9]+)") or 0),
    }

@disk_bp.route('/lifetime_linear', methods=['GET'])
def lifetime_linear():
    """
    线性寿命模型（TBW基础估算）
    """

    raw = get_smart_raw()
    d = parse(raw)

    # NVMe单位换算：1 unit = 512KB
    written_tb = d["written_raw"] * 0.512 / 1024/1024

    # 256GB Apple SSD经验区间
    tbw_est = 200  # 取中值（150~300）

    used_ratio = written_tb / tbw_est
    remaining = max(0, 1 - used_ratio)

    return jsonify({
        "model": "linear_tbw",
        "written_tb": round(written_tb, 2),
        "tbw_estimate": tbw_est,
        "used_ratio": round(used_ratio, 6),
        "remaining_ratio": round(remaining, 6),
        "health_score": round(remaining * 100, 2)
    })

@disk_bp.route('/lifetime_advanced', methods=['GET'])
def lifetime_advanced():
    """
    高级SSD寿命模型：
    - TBW区间
    - WAF估计
    - 非线性衰减
    - 风险曲线
    """

    raw = get_smart_raw()
    d = parse(raw)

    # =========================
    # 写入换算
    # =========================
    written_tb = d["written_raw"] * 0.512 / 1024

    # =========================
    # TBW区间（Apple SSD估算）
    # =========================
    tbw_low = 150
    tbw_high = 300
    tbw_mid = (tbw_low + tbw_high) / 2

    # =========================
    # WAF估计（粗模型）
    # =========================
    host_written_tb = written_tb / 1.3  # 默认写放大1.3
    waf = max(1.0, written_tb / max(host_written_tb, 1e-6))

    # =========================
    # 非线性衰减函数
    # =========================
    used_ratio = float(d["percentage_used"]) / 100

    def nonlinear(x):
        return 1 / (1 + math.exp(-10 * (x - 0.7)))

    nonlinear_health = (1 - nonlinear(used_ratio)) * 100

    # =========================
    # 风险曲线
    # =========================
    risk_base = nonlinear(used_ratio)
    risk = min(1.0, risk_base + d["media_errors"] * 0.2)

    if risk < 0.3:
        risk_level = "LOW"
    elif risk < 0.7:
        risk_level = "MID"
    else:
        risk_level = "HIGH"

    # =========================
    # 寿命倍数（粗略时间外推）
    # =========================
    life_multiplier = 1 / used_ratio if used_ratio > 0 else 999

    return jsonify({
        # ===== 基础 =====
        "written_tb": round(written_tb, 2),
        "percentage_used": used_ratio * 100,

        # ===== TBW =====
        "tbw_range": {
            "low": tbw_low,
            "high": tbw_high,
            "mid": tbw_mid
        },

        # ===== WAF =====
        "waf_estimate": round(waf, 3),

        # ===== 非线性健康 =====
        "nonlinear_health": round(nonlinear_health, 2),

        # ===== 风险 =====
        "risk_score": round(risk, 4),
        "risk_level": risk_level,

        # ===== 寿命倍数 =====
        "life_multiplier": round(life_multiplier, 2)
    })

@disk_bp.route('/space', methods=['GET'])
def disk_space():
    """
    SSD 可用空间分析（df + APFS）
    """

    # =========================
    # 1. df 获取基础空间
    # =========================
    df_raw = subprocess.getoutput("df -h /")

    # =========================
    # 2. APFS container信息
    # =========================
    apfs_raw = subprocess.getoutput("diskutil apfs list")

    def get(pattern, text):
        m = re.search(pattern, text)
        return m.group(1).strip() if m else None

    # =========================
    # 解析 df
    # =========================
    lines = df_raw.splitlines()

    # macOS df格式一般最后一行是 /
    usage_line = lines[-1].split()

    total = usage_line[1]
    used = usage_line[2]
    available = usage_line[3]
    usage_percent = usage_line[4]

    # =========================
    # APFS 可用空间（粗提取）
    # =========================
    apfs_free = None
    apfs_container_match = re.search(r"Capacity Information:(.*?)\n\n", apfs_raw, re.S)

    if apfs_container_match:
        block = apfs_container_match.group(1)
        apfs_free = get(r"Free Space:\s+([0-9.]+\s\w+)", block)

    # =========================
    # 空间压力评估（关键用于WAF模型）
    # =========================
    try:
        usage_num = int(usage_percent.replace("%", ""))
    except:
        usage_num = -1

    if usage_num < 60:
        pressure = "LOW"
    elif usage_num < 80:
        pressure = "MID"
    else:
        pressure = "HIGH"

    return jsonify({
        # ===== df基础 =====
        "total": total,
        "used": used,
        "available": available,
        "usage_percent": usage_percent,

        # ===== APFS =====
        "apfs_free_space": apfs_free,

        # ===== 派生指标 =====
        "space_pressure": pressure,
        "free_ratio_estimate": round(1 - (usage_num / 100), 4) if usage_num != -1 else None
    })