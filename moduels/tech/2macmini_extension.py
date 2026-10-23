"""Mac SSD health dashboard Blueprint.

Register as before::

    from disk_blueprint import disk_bp
    app.register_blueprint(disk_bp)

Compatibility: /disk/info, /smart, /health, /space, /lifetime_linear,
/lifetime_advanced, /raw remain available. The dashboard uses /disk/overview.

Environment variables:
    DISK_HEALTH_DEVICE         default /dev/disk0
    DISK_HEALTH_TBW_TB         optional *documented* rated TBW, decimal TB
    DISK_HEALTH_HISTORY_FILE  optional history JSON file path

Needs macOS, system_profiler, diskutil and smartctl (smartmontools).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify


disk_bp = Blueprint("disk_bp", __name__, url_prefix="/disk")

_DEVICE = os.getenv("DISK_HEALTH_DEVICE", "/dev/disk0")
_DATA_VOLUME = "/System/Volumes/Data"
_CACHE_SECONDS = 30
_HISTORY_INTERVAL = 3600  # maximum one on-disk sample per hour
_HISTORY_DAYS = 90
_lock = threading.RLock()
_cache: dict[str, Any] = {"time": 0.0, "value": None}


def run_cmd(argv: list[str], timeout: int = 12) -> str:
    """Never use shell interpolation; smartctl can return non-zero with valid data."""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
        return p.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _field(raw: str, label: str) -> str | None:
    # Anchored matches avoid incorrectly using a similarly named field.
    m = re.search(r"^\s*" + re.escape(label) + r"\s*:\s*(.*?)\s*$", raw, re.I | re.M)
    return m.group(1) if m and m.group(1) else None


def _integer(raw: str, label: str) -> int | None:
    v = _field(raw, label)
    m = re.match(r"\s*([\d,]+)", v or "")
    return int(m.group(1).replace(",", "")) if m else None


def _percent(raw: str, label: str) -> int | None:
    v = _field(raw, label)
    m = re.match(r"(\d+)\s*%?", v or "")
    return int(m.group(1)) if m else None


def _nvme_units_tb(units: int | None) -> float | None:
    # NVMe SMART: 1 Data Unit = 1,000 x 512 bytes; TB uses decimal 10^12.
    return round(units * 512_000 / 1_000_000_000_000, 4) if units is not None else None


def _fmt_bytes(value: int | None) -> str | None:
    if value is None or value < 0:
        return None
    n = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1000 or suffix == "PB":
            return f"{n:.0f} {suffix}" if suffix == "B" else f"{n:.1f} {suffix}"
        n /= 1000
    return None


def _parse_smart(raw: str) -> dict[str, Any]:
    used = _percent(raw, "Percentage Used")
    temp = _integer(raw, "Temperature")
    spare = _percent(raw, "Available Spare")
    threshold = _percent(raw, "Available Spare Threshold")
    read_units = _integer(raw, "Data Units Read")
    write_units = _integer(raw, "Data Units Written")
    critical_raw = _field(raw, "Critical Warning")
    critical_m = re.search(r"0x[\da-fA-F]+", critical_raw or "")
    critical = critical_m.group(0) if critical_m else None

    health = _field(raw, "SMART overall-health self-assessment test result") or _field(raw, "SMART Health Status")
    supported = any(x is not None for x in (used, temp, spare, critical, read_units, write_units))
    return {
        "available": supported,
        "health": health,
        "critical_warning": critical,
        "temperature_c": temp,
        "percentage_used": used,
        "available_spare": spare,
        "available_spare_threshold": threshold,
        "data_units_read_tb": _nvme_units_tb(read_units),
        "data_units_written_tb": _nvme_units_tb(write_units),
        "data_units_read_raw": str(read_units) if read_units is not None else None,
        "data_units_written_raw": str(write_units) if write_units is not None else None,
        "media_errors": _integer(raw, "Media and Data Integrity Errors"),
        "error_log_entries": _integer(raw, "Error Information Log Entries"),
        "power_on_hours": _integer(raw, "Power On Hours"),
        "power_cycles": _integer(raw, "Power Cycles"),
        "unsafe_shutdowns": _integer(raw, "Unsafe Shutdowns"),
    }


def _parse_info(raw: str) -> dict[str, Any]:
    # system_profiler output is localized; do not assert TRIM=No if absent.
    capacity_raw = _field(raw, "Capacity")
    cap = re.search(r"([\d.,]+)\s*GB", capacity_raw or "", re.I)
    trim_raw = _field(raw, "TRIM Support")
    return {
        "model": _field(raw, "Model"),
        "capacity_gb": float(cap.group(1).replace(",", "")) if cap else None,
        "trim_support": trim_raw,
        "smart_status": _field(raw, "S.M.A.R.T. status") or _field(raw, "SMART Status"),
        "firmware": _field(raw, "Revision"),
    }


def _info_plist(path: str) -> dict[str, Any]:
    output = run_cmd(["diskutil", "info", "-plist", path])
    try:
        return plistlib.loads(output.encode("utf-8")) if output.strip() else {}
    except (ValueError, TypeError, plistlib.InvalidFileException):
        return {}


def _apfs_container_data(data_info: dict[str, Any]) -> tuple[int | None, int | None]:
    # Container free is NOT the same as a volume's df Available on macOS APFS.
    direct_size = data_info.get("APFSContainerSize")
    direct_free = data_info.get("APFSContainerFree")
    if isinstance(direct_size, int) and isinstance(direct_free, int):
        return direct_size, direct_free

    raw = run_cmd(["diskutil", "apfs", "list"])
    if not raw:
        return None, None
    # Select the container containing the data volume; do not select the first
    # container blindly (external APFS disks may be attached).
    blocks = re.split(r"(?=^\+-- Container disk\w+)", raw, flags=re.M)
    disk_id = str(data_info.get("DeviceIdentifier") or "")
    container_ref = str(data_info.get("APFSContainerReference") or "")
    selected = next((b for b in blocks if container_ref and
                     re.search(r"APFS Container Reference:\s*" + re.escape(container_ref) + r"\b", b)), None)
    if selected is None and disk_id:
        selected = next((b for b in blocks if
                         re.search(r"\bVolume\s+" + re.escape(disk_id) + r"\b", b)), None)
    if selected is None:
        return None, None

    def parse_bytes(key: str) -> int | None:
        m = re.search(re.escape(key) + r"\s*:\s*([\d,]+)\s+B\b", selected, re.I)
        return int(m.group(1).replace(",", "")) if m else None

    return (parse_bytes("Size (Capacity Ceiling)"),
            parse_bytes("Capacity Not Allocated") or parse_bytes("Free Space"))


def _parse_df(df_raw: str) -> tuple[int | None, int | None, int | None]:
    # df -k uses 1024-byte units on macOS. Used only as an honest fallback.
    rows = [s.split() for s in df_raw.strip().splitlines()[1:]]
    if not rows:
        return None, None, None
    row = rows[-1]
    if len(row) < 4:
        return None, None, None
    try:
        return tuple(int(row[i]) * 1024 for i in (1, 2, 3))
    except ValueError:
        return None, None, None


def _get_space() -> dict[str, Any]:
    path = _DATA_VOLUME if os.path.exists(_DATA_VOLUME) else "/"
    df_raw = run_cmd(["df", "-k", path])
    df_total, df_used, df_free = _parse_df(df_raw)
    apfs_total, apfs_free = _apfs_container_data(_info_plist(path))
    # Prefer container-level figures for space pressure, but show source.
    if apfs_total is not None and apfs_free is not None and apfs_total > 0:
        total, free, source = apfs_total, apfs_free, "APFS Container"
        used = max(0, total - free)
    else:
        total, used, free, source = df_total, df_used, df_free, "df 数据卷"

    usage = round(100 * used / total, 1) if total and used is not None else None
    pressure = ("HIGH" if usage >= 90 else "MID" if usage >= 80 else "LOW") if usage is not None else None
    return {
        # Existing fields retained for old consumers.
        "total": _fmt_bytes(total),
        "used": _fmt_bytes(used),
        "available": _fmt_bytes(df_free),
        "usage_percent": f"{usage:.1f}%" if usage is not None else None,
        "apfs_free_space": _fmt_bytes(apfs_free),
        "space_pressure": pressure,
        "free_ratio_estimate": round(free / total, 4) if total and free is not None else None,
        # New explicit numeric fields: useful for thresholding and change tracking.
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "df_free_bytes": df_free,
        "apfs_free_bytes": apfs_free,
        "usage_number": usage,
        "source": source,
        "mount_point": path,
    }


def _assess(smart: dict[str, Any], space: dict[str, Any], info: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
    alerts: list[dict[str, str]] = []

    def flag(level: str, title: str, detail: str, action: str):
        alerts.append({"level": level, "title": title, "detail": detail, "action": action})

    if not smart["available"]:
        flag("UNKNOWN", "未获取 SMART 数据", "无法判断磁盘介质健康状态。", "确认 smartctl 已安装并有读取权限。")
    else:
        warning = smart["critical_warning"]
        if warning is not None and int(warning, 16) != 0:
            flag("BAD", "SMART 严重警告", f"设备报告警告位 {warning}。", "优先备份，检查设备及系统日志。")
        spare = smart["available_spare"]
        threshold = smart["available_spare_threshold"]
        if spare is not None and threshold is not None and spare <= threshold:
            flag("BAD", "备用块达到设备阈值", f"可用备用块 {spare}%，阈值 {threshold}%。", "立即备份，准备检修或更换。")
        elif spare is not None and spare <= 15:
            flag("WARN", "备用块偏低", f"可用备用块 {spare}%。", "检查变化趋势并确保备份可用。")
        errors = smart["media_errors"]
        if errors is not None and errors > 0:
            flag("WARN", "存在介质错误记录", f"累计 {errors} 次，需进一步观察是否继续增加。", "检查备份和磁盘日志，关注错误增量。")
        used = smart["percentage_used"]
        if used is not None and used >= 100:
            flag("BAD", "达到厂商标称耐久度指标", f"Percentage Used 为 {used}%。", "确保备份并评估更换，不代表立即失效。")
        elif used is not None and used >= 80:
            flag("WARN", "耐久度使用较高", f"Percentage Used 为 {used}%。", "关注增长速度和备份状态。")
        temp = smart["temperature_c"]
        if temp is not None and temp >= 80:
            flag("BAD", "温度偏高", f"当前 {temp} °C（提示阈值，可按设备规格判断）。", "检查散热与高负载进程。")
        elif temp is not None and temp >= 70:
            flag("WARN", "温度接近高温区", f"当前 {temp} °C。", "查看持续时间并检查通风。")

    free = space["free_bytes"]
    usage = space["usage_number"]
    if free is not None and free < 10 * 1_000_000_000 or (usage is not None and usage >= 95):
        flag("BAD", "磁盘剩余空间紧张", f"剩余 {_fmt_bytes(free) or '未知'}，使用率 {usage if usage is not None else '未知'}%。", "清理可重建缓存/大文件或迁移数据。")
    elif (free is not None and free < 25 * 1_000_000_000) or (usage is not None and usage >= 85):
        flag("WARN", "存储空间需要关注", f"剩余 {_fmt_bytes(free) or '未知'}，使用率 {usage if usage is not None else '未知'}%。", "检查增长较快的目录，预留空间。")

    status_raw = (smart.get("health") or info.get("smart_status") or "").lower()
    if any(x in status_raw for x in ("failed", "failing", "not verified")):
        flag("BAD", "系统报告 SMART 异常", status_raw, "立即备份并检查硬件。")

    priority = {"BAD": 3, "WARN": 2, "UNKNOWN": 1}
    level = max((x["level"] for x in alerts), key=lambda x: priority[x], default="GOOD")
    used = smart["percentage_used"]
    # Not a calibrated failure-probability score: vendor's endurance counter only.
    score = max(0, 100 - used) if used is not None else None
    return {
        "health_score": score,
        "score_kind": "NVMe Percentage Used 的补数，仅表征标称耐久度",
        "percentage_used": used,
        "media_errors": smart["media_errors"],
        "critical_warning": smart["critical_warning"],
        "status": level,
    }, sorted(alerts, key=lambda x: -priority[x["level"]])


def _rated_tbw() -> float | None:
    try:
        v = float(os.getenv("DISK_HEALTH_TBW_TB", ""))
        return v if 0 < v < 1_000_000 else None
    except ValueError:
        return None


def _models(smart: dict[str, Any], health: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    written = smart["data_units_written_tb"]
    tbw = _rated_tbw()
    ratio = round(written / tbw, 6) if tbw and written is not None else None
    linear = {
        "model": "rated_tbw" if tbw else "unconfigured",
        "written_tb": written,
        "tbw_estimate": tbw,
        "used_ratio": ratio,
        "remaining_ratio": max(0, round(1 - ratio, 6)) if ratio is not None else None,
        "health_score": round(max(0, 100 * (1 - ratio)), 2) if ratio is not None else None,
        "note": "须设置设备文档中的 TBW；其估计结果不等于实际剩余寿命。",
    }
    advanced = {
        "written_tb": written,
        "percentage_used": smart["percentage_used"],
        "tbw_range": {"low": None, "high": None, "mid": tbw},
        "waf_estimate": None,  # Host writes != NAND writes; cannot infer WAF.
        "nonlinear_health": None,  # Do not invent a failure probability curve.
        "risk_score": None,
        "risk_level": "HIGH" if health["status"] == "BAD" else ("MID" if health["status"] == "WARN" else ("LOW" if health["status"] == "GOOD" else "UNKNOWN")),
        "life_multiplier": None,
        "note": "风险由 SMART 警告、介质错误、耐久度与温度触发；WAF/寿命倍数无实测依据。",
    }
    return linear, advanced


def _collect() -> dict[str, Any]:
    now = time.monotonic()
    with _lock:
        if _cache["value"] is not None and now - _cache["time"] < _CACHE_SECONDS:
            return _cache["value"]
        raw_smart = run_cmd(["smartctl", "-a", _DEVICE])
        raw_info = run_cmd(["system_profiler", "SPNVMeDataType"])
        smart = _parse_smart(raw_smart)
        info = _parse_info(raw_info)
        space = _get_space()
        health, alerts = _assess(smart, space, info)
        linear, advanced = _models(smart, health)
        data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "smart": smart, "info": info, "space": space,
            "health": health, "alerts": alerts,
            "lifetime_linear": linear, "lifetime_advanced": advanced,
        }
        _cache.update({"time": time.monotonic(), "value": data})
        return data


def _history_path() -> Path:
    return Path(os.getenv("DISK_HEALTH_HISTORY_FILE", str(Path.home() / ".local/state/disk-health/history.json"))).expanduser()


def _history(data: dict[str, Any]) -> dict[str, Any]:
    """Local, compact trend history, only non-identifying metrics, 1 sample/hour."""
    with _lock:
        path = _history_path()
        try:
            history = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
            if not isinstance(history, list):
                history = []
        except (OSError, ValueError):
            history = []
        now = time.time()
        history = [r for r in history if isinstance(r, dict) and isinstance(r.get("time"), (float, int))
                   and now - _HISTORY_DAYS * 86400 <= r["time"] <= now + 60]
        latest = {
            "time": now,
            "free_bytes": data["space"]["free_bytes"],
            "written_tb": data["smart"]["data_units_written_tb"],
            "media_errors": data["smart"]["media_errors"],
            "percentage_used": data["smart"]["percentage_used"],
            "temperature_c": data["smart"]["temperature_c"],
        }
        if not history or now - history[-1]["time"] >= _HISTORY_INTERVAL:
            # Don't record empty snapshots that would distort later calculations.
            if any(latest[k] is not None for k in ("free_bytes", "written_tb", "percentage_used")):
                history.append(latest)
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(path)
                except OSError:
                    pass  # History is optional; live metrics must still work.

        def diff(old: dict[str, Any] | None) -> dict[str, Any] | None:
            if old is None:
                return None
            d: dict[str, Any] = {"hours": round((now - old["time"]) / 3600, 1)}
            for key in ("free_bytes", "written_tb", "media_errors", "percentage_used"):
                a, b = latest.get(key), old.get(key)
                n = round(a - b, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
                # Counters can reset after replacing the SSD; don't report bogus negative writes.
                if key in ("written_tb", "media_errors", "percentage_used") and n is not None and n < 0:
                    n = None
                d[key] = n
            return d

        def baseline(hours: float):
            return next((r for r in reversed(history) if now - r["time"] >= hours * 3600), None)

        previous = history[-2] if len(history) >= 2 else None
        return {
            "points": history[-90:],
            "delta_24h": diff(baseline(20)),
            "delta_7d": diff(baseline(6 * 24)),
            "latest_change": diff(previous),
            "count": len(history),
        }


# Keep the existing Blueprint / URLs intact.
@disk_bp.route("/overview", methods=["GET"])
def disk_overview():
    snapshot = _collect().copy()
    snapshot["history"] = _history(snapshot)
    return jsonify(snapshot)


@disk_bp.route("/info", methods=["GET"])
def disk_info():
    return jsonify(_collect()["info"])


@disk_bp.route("/smart", methods=["GET"])
def disk_smart():
    return jsonify(_collect()["smart"])


@disk_bp.route("/health", methods=["GET"])
def disk_health():
    return jsonify(_collect()["health"])


@disk_bp.route("/lifetime_linear", methods=["GET"])
def lifetime_linear():
    return jsonify(_collect()["lifetime_linear"])


@disk_bp.route("/lifetime_advanced", methods=["GET"])
def lifetime_advanced():
    return jsonify(_collect()["lifetime_advanced"])


@disk_bp.route("/space", methods=["GET"])
def disk_space():
    return jsonify(_collect()["space"])


@disk_bp.route("/history", methods=["GET"])
def disk_history():
    return jsonify(_history(_collect()))


@disk_bp.route("/raw", methods=["GET"])
def disk_raw():
    # Raw output can contain device IDs; only request it when the user opens diagnostics.
    return jsonify({
        "system_profiler": run_cmd(["system_profiler", "SPNVMeDataType"]),
        "smartctl": run_cmd(["smartctl", "-a", _DEVICE]),
    })
