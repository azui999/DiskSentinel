"""历史对比与异常判断。

原则（对齐 docs/设计.md 第四节）：
  * 不是所有指标都"大于 0 就报警"，不同类型用不同策略；
  * 只看增量会比只看绝对值更早发现问题；
  * 不给出玄学健康分，只给可解释的结论。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import normalize

LEVEL_OK = "ok"
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"

_LEVEL_ORDER = {LEVEL_OK: 0, LEVEL_INFO: 1, LEVEL_WARN: 2, LEVEL_CRIT: 3}
_LEVEL_TEXT = {LEVEL_OK: "正常", LEVEL_INFO: "提示", LEVEL_WARN: "注意", LEVEL_CRIT: "警告"}

# 绝对阈值：与增量无关的判断
TEMPERATURE_WARN = 50
TEMPERATURE_CRIT = 55
LIFE_USED_WARN = 80
SPARE_WARN = 20


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""

    @property
    def level_text(self) -> str:
        return _LEVEL_TEXT.get(self.level, self.level)


def _prev_row(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(history) >= 2:
        return history[-2]
    return None


def _delta(current: Any, previous: Any) -> float | None:
    if current is None or previous is None:
        return None
    if isinstance(current, (int, float)) and isinstance(previous, (int, float)):
        return current - previous
    return None


def analyze(history: list[dict[str, Any]]) -> list[Finding]:
    """history 为按时间升序的记录，最后一条为本次。"""
    if not history:
        return [Finding(LEVEL_INFO, "尚无检测记录", "点击【立即检测】保存第一份快照。")]

    current = history[-1]
    previous = _prev_row(history)
    findings: list[Finding] = []

    if previous is None:
        findings.append(
            Finding(LEVEL_INFO, "首次记录，暂无对比基线", "积累第二次快照后才能判断趋势。")
        )

    findings.extend(_critical_rules(current, previous))
    findings.extend(_interface_rules(current, previous))
    findings.extend(_wear_rules(current, previous))
    findings.extend(_runtime_rules(current, previous))
    findings.extend(_general_rules(current))

    if not findings:
        findings.append(Finding(LEVEL_OK, "未发现异常", "所有受监控指标与上次检测相比无变化。"))
    return findings


def _critical_rules(current: dict[str, Any], previous: dict[str, Any] | None) -> list[Finding]:
    out: list[Finding] = []
    rules = {
        "reallocated_sectors": ("重映射扇区计数", "已出现重映射扇区，说明硬盘开始用备用扇区顶替坏道。建议备份重要数据并持续观察。"),
        "pending_sectors": ("待映射扇区数", "存在待映射扇区，这些扇区读取异常但尚未重映射，风险高于重映射。建议尽快备份。"),
        "uncorrectable_sectors": ("不可校正扇区数", "存在无法纠正的读错误，数据可能已受损。请立即备份并考虑更换。"),
        "media_errors": ("介质错误", "NVMe 控制器报告介质级错误，属于不可恢复错误计数。"),
        "runtime_bad_block": ("运行时坏块", "运行时新增坏块计数。"),
        "added_bad_flash_block": ("新增坏块", "闪存新增坏块数量。"),
        "ecc_errors": ("ECC 纠错次数", "出现 ECC 纠错，说明读取过程需要纠错介入。"),
    }

    for key, (label, advice) in rules.items():
        value = current.get(key)
        if not isinstance(value, (int, float)):
            continue
        prev_value = previous.get(key) if previous else None
        change = _delta(value, prev_value)

        if value <= 0:
            continue
        if previous is None:
            # 首次采集没有基线。只有"本应为 0"的指标才值得当场提醒，
            # 其余累计型计数（重映射、坏块、ECC）先记录基线，等第二次再判断增长。
            strict = key in ("pending_sectors", "uncorrectable_sectors", "media_errors")
            out.append(
                Finding(
                    LEVEL_WARN if strict else LEVEL_INFO,
                    f"{label}为 {normalize.format_value(key, value)}（首次记录）",
                    advice,
                )
            )
            continue

        if change is None:
            out.append(Finding(LEVEL_WARN, f"{label}为 {normalize.format_value(key, value)}", advice))
        elif change > 0:
            level = LEVEL_CRIT if key in ("pending_sectors", "uncorrectable_sectors") else LEVEL_WARN
            out.append(
                Finding(
                    level,
                    f"{label}较上次增加 {normalize.format_value(key, change)}",
                    f"当前 {normalize.format_value(key, value)}，上次 {normalize.format_value(key, prev_value)}。{advice}",
                )
            )
        elif change < 0:
            out.append(
                Finding(
                    LEVEL_INFO,
                    f"{label}较上次下降 {normalize.format_value(key, abs(change))}",
                    "数值下降通常是厂商重映射或计数回绕导致，仍建议保持观察。",
                )
            )

    warning = current.get("critical_warning")
    if isinstance(warning, (int, float)) and warning:
        out.append(
            Finding(LEVEL_CRIT, f"NVMe 严重告警标志为 {int(warning)}", "控制器报告可用备用块耗尽、可靠性下降或只读模式等状态。")
        )
    return out


def _interface_rules(current: dict[str, Any], previous: dict[str, Any] | None) -> list[Finding]:
    """接口类只看新增，历史累计值本身不代表硬盘要坏。"""
    out: list[Finding] = []
    for key, label, advice in (
        ("crc_errors", "接口 CRC 错误", "多为 SATA 线缆、接口氧化或供电不稳导致，建议更换数据线。"),
        ("num_err_log_entries", "错误日志条目", "控制器记录了新的错误日志，可结合原始日志进一步判断。"),
    ):
        value = current.get(key)
        prev_value = previous.get(key) if previous else None
        change = _delta(value, prev_value)
        if change is None or change <= 0:
            continue
        out.append(
            Finding(
                LEVEL_WARN,
                f"{label}新增 {normalize.format_value(key, change)}",
                f"累计 {normalize.format_value(key, value)}。{advice}",
            )
        )
    return out


def _wear_rules(current: dict[str, Any], previous: dict[str, Any] | None) -> list[Finding]:
    out: list[Finding] = []

    used = current.get("life_used")
    if isinstance(used, (int, float)) and used >= LIFE_USED_WARN:
        level = LEVEL_CRIT if used >= 95 else LEVEL_WARN
        out.append(
            Finding(level, f"寿命已消耗 {used}%", "接近厂商建议的写入寿命上限，建议规划更换。")
        )

    spare = current.get("available_spare")
    if isinstance(spare, (int, float)) and spare < SPARE_WARN:
        out.append(
            Finding(LEVEL_WARN, f"可用备用块仅剩 {spare}%", "备用块消耗过多，写入性能与可靠性可能下降。")
        )

    for key, label in (("program_fail", "编程失败"), ("erase_fail", "擦除失败")):
        value = current.get(key)
        prev_value = previous.get(key) if previous else None
        change = _delta(value, prev_value)
        if change and change > 0:
            out.append(
                Finding(
                    LEVEL_WARN,
                    f"{label}增加 {normalize.format_value(key, change)}",
                    f"累计 {normalize.format_value(key, value)}。",
                )
            )

    if previous:
        written_now = current.get("lba_written")
        written_before = previous.get("lba_written")
        change = _delta(written_now, written_before)
        if change and change > 0:
            out.append(
                Finding(
                    LEVEL_INFO,
                    f"本周期写入 {change:,} 个 LBA",
                    f"累计 {written_now:,}。该指标用于估算写入量，不是健康判据。",
                )
            )
    return out


def _runtime_rules(current: dict[str, Any], previous: dict[str, Any] | None) -> list[Finding]:
    out: list[Finding] = []

    temp = current.get("temperature")
    if isinstance(temp, (int, float)):
        if temp >= TEMPERATURE_CRIT:
            out.append(Finding(LEVEL_WARN, f"温度 {temp}°C，偏高", "建议改善散热：增加风道、清理灰尘或降低环境温度。"))
        elif temp >= TEMPERATURE_WARN:
            out.append(Finding(LEVEL_INFO, f"温度 {temp}°C", "略高但仍在常见范围内，注意观察长期趋势。"))

    hours = current.get("power_on_hours")
    if isinstance(hours, (int, float)):
        prev_hours = previous.get("power_on_hours") if previous else None
        change = _delta(hours, prev_hours)
        if change is not None and change < 0:
            out.append(Finding(LEVEL_INFO, "通电时间较上次减少", "可能是计数器重置或读取到了不同设备。"))
        elif change is not None and change > 0:
            out.append(
                Finding(
                    LEVEL_INFO,
                    f"通电时间 {int(hours):,} 小时（本次增加 {int(change)} 小时）",
                    "",
                )
            )
    return out


def _general_rules(current: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    severity = str(current.get("severity") or "")
    if severity == "error":
        out.append(Finding(LEVEL_CRIT, "SMART 总体状态为失败", "设备自身判定健康状态异常，请立即备份。"))
    exit_status = current.get("exit_status")
    if isinstance(exit_status, int) and exit_status & 0x40:
        out.append(Finding(LEVEL_INFO, "设备错误日志中存在记录", "可到【原始数据】页查看详细信息。"))
    return out


def summarize(findings: list[Finding]) -> tuple[str, str]:
    """返回 (总体等级, 一句话结论)。"""
    if not findings:
        return LEVEL_OK, "未发现异常。"
    worst = max(findings, key=lambda f: _LEVEL_ORDER.get(f.level, 0))
    if worst.level == LEVEL_CRIT:
        return LEVEL_CRIT, worst.title
    if worst.level == LEVEL_WARN:
        return LEVEL_WARN, worst.title
    if worst.level == LEVEL_INFO:
        return LEVEL_INFO, "数据已记录，暂未见风险信号。"
    return LEVEL_OK, "未发现异常。"
