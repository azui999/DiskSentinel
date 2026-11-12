"""把 ATA 与 NVMe 两种完全不同的 SMART 结构映射成同一套指标。

上层（UI / 分析 / CSV）只认这里的 key，不直接碰 smartctl 的原始字段。
新增盘型（SAS、USB 桥接）时只需要在这里补映射。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 指标分组决定分析策略，不能混用：
#   critical  —— 出现非零或增长都应关注（坏道类）
#   interface —— 只看增量，历史累计值本身没意义
#   wear      —— 磨损/寿命类，看趋势和阈值
#   runtime   —— 运行状态类，看绝对值与趋势
GROUP_CRITICAL = "critical"
GROUP_INTERFACE = "interface"
GROUP_WEAR = "wear"
GROUP_RUNTIME = "runtime"


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    unit: str
    group: str
    plot: bool = True


# 顺序即 CSV 列顺序，也是界面里的展示顺序。只增不改。
METRIC_DEFS: tuple[MetricDef, ...] = (
    MetricDef("reallocated_sectors", "重映射扇区计数", "个", GROUP_CRITICAL),
    MetricDef("pending_sectors", "待映射扇区数", "个", GROUP_CRITICAL),
    MetricDef("uncorrectable_sectors", "不可校正扇区数", "个", GROUP_CRITICAL),
    MetricDef("media_errors", "介质错误", "次", GROUP_CRITICAL),
    MetricDef("runtime_bad_block", "运行时坏块", "块", GROUP_CRITICAL),
    MetricDef("added_bad_flash_block", "新增坏块", "块", GROUP_CRITICAL),
    MetricDef("ecc_errors", "ECC 纠错次数", "次", GROUP_CRITICAL),
    MetricDef("critical_warning", "严重告警标志", "", GROUP_CRITICAL, plot=False),
    MetricDef("crc_errors", "接口 CRC 错误", "次", GROUP_INTERFACE),
    MetricDef("num_err_log_entries", "错误日志条目", "条", GROUP_INTERFACE),
    MetricDef("temperature", "温度", "°C", GROUP_RUNTIME),
    MetricDef("power_on_hours", "通电时间", "小时", GROUP_RUNTIME),
    MetricDef("power_cycles", "通电次数", "次", GROUP_RUNTIME),
    MetricDef("unsafe_shutdowns", "非安全关机", "次", GROUP_RUNTIME),
    MetricDef("por_recovery", "异常断电恢复", "次", GROUP_RUNTIME),
    MetricDef("load_cycle", "磁头加载次数", "次", GROUP_RUNTIME),
    MetricDef("start_stop", "启停次数", "次", GROUP_RUNTIME),
    MetricDef("life_used", "寿命消耗", "%", GROUP_WEAR),
    MetricDef("ssd_life_left", "剩余寿命", "%", GROUP_WEAR),
    MetricDef("wear_leveling", "磨损度计数", "", GROUP_WEAR),
    MetricDef("available_spare", "可用备用块", "%", GROUP_WEAR),
    MetricDef("used_reserved_blocks", "已用备用块", "块", GROUP_WEAR),
    MetricDef("program_fail", "编程失败", "次", GROUP_WEAR),
    MetricDef("erase_fail", "擦除失败", "次", GROUP_WEAR),
    MetricDef("lba_written", "累计写入 LBA", "", GROUP_WEAR),
)

DEF_BY_KEY = {d.key: d for d in METRIC_DEFS}
METRIC_KEYS: tuple[str, ...] = tuple(d.key for d in METRIC_DEFS)

# 每个指标的人话解释，用作界面 tooltip。写"是什么 + 怎么判断"。
METRIC_INFO: dict[str, str] = {
    "reallocated_sectors": "坏道已被备用扇区顶替的累计数量。0 最理想；出现后不增长说明已稳定，持续增长说明盘面在继续坏，应尽快备份。",
    "pending_sectors": "读取不稳定、等待重映射的扇区。非零就需要警惕，持续增长说明坏道在扩散，建议立即备份。",
    "uncorrectable_sectors": "无法纠正的读错误扇区。非零代表数据可能已经受损，是最严重的信号之一。",
    "media_errors": "NVMe 闪存介质级错误计数。少量历史累计可能正常，重点看是否增长。",
    "runtime_bad_block": "运行过程中新发现的坏块数量，重点看是否持续增长。",
    "added_bad_flash_block": "闪存新增坏块数量，累计值包含出厂记录，重点看增长而非绝对值。",
    "ecc_errors": "读取时由纠错机制修复的次数。有纠错不代表数据损坏，只有持续大幅增长才需要关注。",
    "critical_warning": "NVMe 控制器健康位图：备用块耗尽、可靠性下降、进入只读等。非 0 应立即处理。",
    "crc_errors": "SATA 线缆/接口的传输校验错误。只看新增：突然增加说明线缆接触不良或供电不稳，通常与硬盘本体无关。",
    "num_err_log_entries": "控制器记录的错误日志条数，包含历史遗留，重点看增量。",
    "temperature": "盘体当前温度。长期超过 55°C 会加速老化，建议改善机箱风道。",
    "power_on_hours": "硬盘累计通电工作的小时数，用来估算使用年限。",
    "power_cycles": "完整的通电/断电循环次数。",
    "unsafe_shutdowns": "未正常关机（直接断电、强制复位）的次数，可能损伤文件系统。",
    "por_recovery": "异常断电后恢复运行的次数。",
    "load_cycle": "磁头加载/卸载次数（机械盘相关），部分型号此值增长很快属于正常设计。",
    "start_stop": "电机启停次数。",
    "life_used": "按写入量/磨损估算的寿命消耗百分比，100% 表示到达厂商标称寿命上限。",
    "ssd_life_left": "厂商估算的剩余寿命百分比。",
    "wear_leveling": "磨损均衡相关计数（厂商自定义），反映闪存块的磨损分布。",
    "available_spare": "可用备用块容量百分比，低于厂商阈值（常见 10%）会触发严重告警。",
    "used_reserved_blocks": "已启用的备用块数量。",
    "program_fail": "闪存编程失败次数，非零增长说明闪存单元开始出问题。",
    "erase_fail": "闪存擦除失败次数，同编程失败。",
    "lba_written": "累计写入的逻辑扇区数量，用来估算总写入量，本身不是健康判据。",
}

GROUP_INFO: dict[str, str] = {
    GROUP_CRITICAL: "关键指标：直接关系数据安全，出现非零或增长都值得留意",
    GROUP_INTERFACE: "接口指标：历史累计意义不大，重点看有没有新增",
    GROUP_WEAR: "磨损指标：与写入寿命相关，看趋势和剩余余量",
    GROUP_RUNTIME: "运行状态指标：看绝对值和长期趋势",
}


# ATA 属性 ID → 统一 key。按 PRIORITY 顺序填充，先到先得。
ATA_ID_MAP: dict[int, str] = {
    5: "reallocated_sectors",
    197: "pending_sectors",
    198: "uncorrectable_sectors",
    187: "uncorrectable_sectors",   # 三星 SSD 用它表示不可校正错误
    199: "crc_errors",
    194: "temperature",
    190: "temperature",
    9: "power_on_hours",
    12: "power_cycles",
    193: "load_cycle",
    4: "start_stop",
    177: "wear_leveling",
    231: "ssd_life_left",
    179: "used_reserved_blocks",
    181: "program_fail",
    182: "erase_fail",
    183: "runtime_bad_block",
    252: "added_bad_flash_block",
    195: "ecc_errors",
    235: "por_recovery",
    241: "lba_written",
}

ATA_PRIORITY: tuple[int, ...] = (
    5, 197, 198, 187, 199, 194, 190, 9, 12, 193, 4,
    177, 231, 179, 181, 182, 183, 252, 195, 235, 241,
)


def _num(value: Any) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else float(value)
    return None


def _from_ata(raw: dict[str, Any], out: dict[str, Any]) -> None:
    table = (raw.get("ata_smart_attributes") or {}).get("table") or []
    by_id: dict[int, dict[str, Any]] = {}
    for item in table:
        attr_id = item.get("id")
        if isinstance(attr_id, int):
            by_id[attr_id] = item

    for attr_id in ATA_PRIORITY:
        key = ATA_ID_MAP[attr_id]
        if key in out:
            continue
        item = by_id.get(attr_id)
        if item is None:
            continue
        raw_block = item.get("raw") or {}
        value = _num(raw_block.get("value"))
        if value is None:
            # 部分属性 raw.value 为 0 而真实值在字符串里（形如 "0/0" 或 "12 (Min/Max 10/40)"）
            text = str(raw_block.get("string") or "").strip()
            head = text.split()[0] if text else ""
            try:
                value = int(float(head))
            except (TypeError, ValueError):
                value = None
        if value is None:
            continue
        out[key] = value


def _from_nvme(raw: dict[str, Any], out: dict[str, Any]) -> None:
    log = raw.get("nvme_smart_health_information_log") or {}
    mapping = {
        "media_errors": "media_errors",
        "critical_warning": "critical_warning",
        "available_spare": "available_spare",
        "percentage_used": "life_used",
        "unsafe_shutdowns": "unsafe_shutdowns",
        "num_err_log_entries": "num_err_log_entries",
        "temperature": "temperature",
    }
    for src, key in mapping.items():
        if key in out:
            continue
        value = _num(log.get(src))
        if value is not None:
            out[key] = value


def _from_common(raw: dict[str, Any], out: dict[str, Any]) -> None:
    power_on = (raw.get("power_on_time") or {}).get("hours")
    value = _num(power_on)
    if value is not None and "power_on_hours" not in out:
        out["power_on_hours"] = value

    cycles = _num(raw.get("power_cycle_count"))
    if cycles is not None and "power_cycles" not in out:
        out["power_cycles"] = cycles

    temp = _num((raw.get("temperature") or {}).get("current"))
    if temp is not None and "temperature" not in out:
        out["temperature"] = temp

    spare = _num((raw.get("spare_available") or {}).get("current_percent"))
    if spare is not None and "available_spare" not in out:
        out["available_spare"] = spare

    used = _num((raw.get("endurance_used") or {}).get("current_percent"))
    if used is not None and "life_used" not in out:
        out["life_used"] = used

    # ATA 的 endurance_used 在不同权限下读数不稳定（实测 0% / 1% 漂移）。
    # 与 CrystalDiskInfo 一致：用磨损计数(177)的"归一化值"反推寿命消耗。
    # 注意只能用 value（归一化 0~100），不能用 raw（三星盘上是累计次数）。
    if not out.get("life_used"):
        for item in (raw.get("ata_smart_attributes") or {}).get("table") or []:
            if item.get("id") == 177:
                value = _num(item.get("value"))
                if value is not None and 0 <= value <= 100:
                    out["life_used"] = max(0, min(100, round(100 - value)))
                break


def extract_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    """原始 JSON → 统一指标字典（只包含真实取到的键）。"""
    out: dict[str, Any] = {}
    if "ata_smart_attributes" in raw:
        _from_ata(raw, out)
    if "nvme_smart_health_information_log" in raw:
        _from_nvme(raw, out)
    _from_common(raw, out)
    return out


def label_of(key: str) -> str:
    definition = DEF_BY_KEY.get(key)
    return definition.label if definition else key


def unit_of(key: str) -> str:
    definition = DEF_BY_KEY.get(key)
    return definition.unit if definition else ""


def group_of(key: str) -> str:
    definition = DEF_BY_KEY.get(key)
    return definition.group if definition else GROUP_RUNTIME


def info_of(key: str) -> str:
    """指标的人话解释（tooltip 用）。"""
    text = METRIC_INFO.get(key)
    if text:
        return text
    definition = DEF_BY_KEY.get(key)
    return GROUP_INFO.get(definition.group, "") if definition else ""


def ata_attr_info(attr_id: int) -> str:
    """ATA 属性 ID → 人话解释，未收录的返回空串。"""
    key = ATA_ID_MAP.get(attr_id)
    return info_of(key) if key else ""


# NVMe 健康日志字段的人话解释（属性表 tooltip 用）
NVME_INFO: dict[str, str] = {
    "critical_warning": "严重告警位图：备用块耗尽、可靠性下降、进入只读等，非 0 应立即处理",
    "temperature": "当前温度（°C）",
    "available_spare": "可用备用块百分比",
    "available_spare_threshold": "备用块告警阈值（低于即告警）",
    "percentage_used": "寿命消耗百分比，100% 表示到达厂商标称寿命",
    "data_units_read": "累计读取量，单位 1000 个 512 字节扇区",
    "data_units_written": "累计写入量，单位 1000 个 512 字节扇区",
    "host_reads": "主机读取次数",
    "host_writes": "主机写入次数",
    "controller_busy_time": "控制器繁忙时间（分钟）",
    "power_cycles": "通电循环次数",
    "power_on_hours": "通电小时数",
    "unsafe_shutdowns": "非安全关机次数",
    "media_errors": "介质级错误数量",
    "num_err_log_entries": "错误日志条数",
    "warning_temp_time": "处于警告温度的累计时间（分钟）",
    "critical_comp_time": "处于临界温度的累计时间（分钟）",
}


def group_info_of(group: str) -> str:
    return GROUP_INFO.get(group, "")


def host_written_bytes(raw: dict[str, Any]) -> int | None:
    """主机累计写入量（字节）。

    ATA：ID 241 的原始值 × 512 字节；NVMe：data_units_written × 512000 字节。
    用于对标 CrystalDiskInfo 的"主机总计写入"。
    """
    table = (raw.get("ata_smart_attributes") or {}).get("table") or []
    for item in table:
        if item.get("id") == 241:
            value = (item.get("raw") or {}).get("value")
            if isinstance(value, (int, float)) and value > 0:
                return int(value * 512)
    log = raw.get("nvme_smart_health_information_log") or {}
    units = log.get("data_units_written")
    if isinstance(units, (int, float)) and units > 0:
        return int(units * 512000)
    return None


def format_value(key: str, value: Any) -> str:
    if value is None:
        return "—"
    unit = unit_of(key)
    if isinstance(value, float):
        text = f"{value:,.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{value:,}"
    if not unit:
        return text
    if unit in ("°C", "%"):
        return f"{text}{unit}"
    return f"{text} {unit}"
