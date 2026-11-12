"""把 SMART 指标和趋势翻译成人话报告。

设计原则（沿用 docs/设计.md）：
  * 不给玄学健康分，只给"结论 + 依据 + 建议"；
  * 依据必须能对上具体指标，用户能自己去【属性表】核对；
  * 结论优先于参数——先告诉用户这块盘能不能继续用、要不要备份，
    再解释是哪些数据支撑这个判断；
  * 区分"硬盘要坏"和"线缆/供电有问题"，两者的建议完全不同。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import normalize

LEVEL_ORDER = {"ok": 0, "info": 1, "warn": 2, "crit": 3}
LEVEL_TEXT = {"ok": "良好", "info": "提示", "warn": "注意", "crit": "警告"}

# 坏道类：一旦出现就是"盘面/闪存已经在损失可用容量"
BAD_SECTOR_KEYS = {
    "reallocated_sectors": "重映射扇区",
    "pending_sectors": "待映射扇区",
    "uncorrectable_sectors": "不可校正扇区",
    "media_errors": "介质错误",
    "runtime_bad_block": "运行时坏块",
    "added_bad_flash_block": "新增坏块",
}
# 这几项意味着"数据此刻就可能读不出来"，非零就要报出来
SEVERE_BAD_KEYS = ("pending_sectors", "uncorrectable_sectors", "media_errors")
# 累计型计数：裸值的语义由厂商定义（例如三星 252 的裸值是累计值，
# 固件归一化值仍是 100/阈值 10），只能看"是否增长"，绝对值不能当结论。
CUMULATIVE_KEYS = (
    "reallocated_sectors",
    "runtime_bad_block",
    "added_bad_flash_block",
    "ecc_errors",
)

TEMPERATURE_INFO = 50
TEMPERATURE_WARN = 55
LIFE_USED_INFO = 80
LIFE_USED_WARN = 90
SPARE_WARN = 10


@dataclass
class Basis:
    """一条判断依据：指标 -> 实测值 -> 为什么重要。"""

    label: str
    value: str
    why: str
    level: str = "info"


@dataclass
class Report:
    level: str
    headline: str
    action: str
    basis: list[Basis] = field(default_factory=list)
    trends: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    meta: list[str] = field(default_factory=list)

    @property
    def level_text(self) -> str:
        return LEVEL_TEXT.get(self.level, self.level)

    @property
    def text(self) -> str:
        """纯文本版，方便整段复制给别人或大模型。"""
        lines = [f"硬盘健康报告", f"结论：{self.headline}", f"建议行动：{self.action}", ""]
        if self.basis:
            lines.append("一、判断依据")
            lines.extend(f"- {b.label}：{b.value}。{b.why}" for b in self.basis)
            lines.append("")
        if self.trends:
            lines.append("二、趋势观察")
            lines.extend(f"- {t}" for t in self.trends)
            lines.append("")
        if self.advice:
            lines.append("三、建议")
            lines.extend(f"{i}. {a}" for i, a in enumerate(self.advice, 1))
            lines.append("")
        if self.meta:
            lines.append("数据说明：" + "；".join(self.meta))
        return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------- #
# 小工具
# ---------------------------------------------------------------------- #
def _num(row: dict[str, Any] | None, key: str) -> float | None:
    if not row:
        return None
    value = row.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fmt(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    return normalize.format_value(key, int(value) if float(value).is_integer() else value)


def _fmt_bytes(size: float | None) -> str:
    if size is None or size <= 0:
        return "—"
    units = [("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)]
    for unit, scale in units:
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{int(size)} B"


def _when(row: dict[str, Any]) -> str:
    text = str(row.get("timestamp") or "").replace("T", " ")
    return text[:16] if text else "未知时间"


def _nice_date(row: dict[str, Any]) -> str:
    text = str(row.get("timestamp") or "")
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10] or "未知日期"


def _first_nonzero(history: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    for row in history:
        value = _num(row, key)
        if value is not None and value > 0:
            return row
    return None


def _span_text(history: list[dict[str, Any]]) -> str:
    if len(history) < 2:
        return ""
    try:
        start = datetime.fromisoformat(str(history[0].get("timestamp")))
        end = datetime.fromisoformat(str(history[-1].get("timestamp")))
    except ValueError:
        return ""
    days = (end - start).total_seconds() / 86400
    if days < 1:
        hours = max(1, round(days * 24))
        return f"{hours} 小时"
    return f"{days:.0f} 天"


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def build(
    history: list[dict[str, Any]],
    raw: dict[str, Any] | None = None,
    model: str = "",
    is_ssd: bool | None = None,
) -> Report:
    """history 按时间升序，最后一条为本次。"""
    if not history:
        return Report(
            level="info",
            headline="这块硬盘还没有任何检测记录。",
            action="点击【立即检测】保存第一份快照，之后这里会自动给出可读的健康结论。",
            advice=["先做一次检测，攒到 2 次以上才能看趋势。"],
        )

    current = history[-1]
    previous = history[-2] if len(history) >= 2 else None
    basis: list[Basis] = []
    levels: list[str] = []

    def add(label: str, value: str, why: str, level: str) -> None:
        basis.append(Basis(label, value, why, level))
        levels.append(level)

    # 1) 固件自己的判定最优先 ------------------------------------------------
    severity = str(current.get("severity") or "")
    if severity == "error":
        add(
            "硬盘自检结果",
            "失败",
            "这是硬盘固件自己给出的结论，说明它认为自身已不可靠，优先级高于任何单项指标。",
            "crit",
        )
    warning = _num(current, "critical_warning")
    if warning:
        add(
            "NVMe 严重告警标志",
            str(int(warning)),
            "控制器在报告可用备用块耗尽、可靠性下降或已进入只读模式等状态。",
            "crit",
        )

    # 2) 坏道类 ------------------------------------------------------------
    # 2a. 决定性指标：非零即代表"有数据现在可能读不出来"
    for key in SEVERE_BAD_KEYS:
        value = _num(current, key)
        if value is None or value <= 0:
            continue
        first_row = _first_nonzero(history, key)
        since = f"，最早出现在 {_nice_date(first_row)} 的检测" if first_row else ""
        add(
            BAD_SECTOR_KEYS[key],
            _fmt(key, value),
            f"这些扇区目前读不出来，硬盘还没决定是否用备用扇区顶替{since}。"
            "已写入的文件可能已经损坏，并且数量可能继续增加。",
            "crit",
        )

    # 2b. 累计型计数：只在"确实变多了"时报警，绝对值不作为判据
    for key in CUMULATIVE_KEYS:
        value = _num(current, key)
        if value is None or value <= 0:
            continue
        label = BAD_SECTOR_KEYS.get(key) or normalize.label_of(key)
        prev_value = _num(previous, key)
        first_row = _first_nonzero(history, key)
        since = f"，最早出现在 {_nice_date(first_row)} 的检测" if first_row else ""

        if prev_value is not None and value > prev_value:
            severe = key in ("reallocated_sectors", "runtime_bad_block")
            add(
                label,
                f"{_fmt(key, value)}（上次 {_fmt(key, prev_value)}）",
                f"该计数比上次检测又增加了{since}。持续增长说明介质在继续损耗，"
                "不是一次偶发读取失败。",
                "crit" if severe else "warn",
            )
        elif key == "reallocated_sectors":
            # 重映射扇区是最被广泛认可的坏道信号，即使已稳定也值得备注一句
            add(
                label,
                _fmt(key, value),
                f"硬盘曾经用备用扇区顶替过坏道{since}，目前数量没有继续增加。"
                "容量和性能通常不受影响，但这块盘已经不算「新盘」，需要定期盯着。",
                "info",
            )
        # 其余累计型计数（新增坏块 / ECC）绝对值语义由厂商定义，
        # 没有增长就不下结论，避免把固件累计值误读成坏块。 

    # 3) 接口类：是线的问题，不是盘的问题 -----------------------------------
    crc = _num(current, "crc_errors")
    if crc is not None and crc > 0:
        first_row = _first_nonzero(history, "crc_errors")
        prev_crc = _num(previous, "crc_errors")
        since = f"，最早出现在 {_nice_date(first_row)} 的检测" if first_row else ""
        if prev_crc is not None and crc > prev_crc:
            add(
                "接口 CRC 错误",
                f"{_fmt('crc_errors', crc)}（本次新增 {_fmt('crc_errors', crc - prev_crc)}）",
                f"数据传输过程中出现校验失败并重传{since}。这类错误多数来自 SATA 线缆老化、"
                "接口氧化或供电不稳，通常换根数据线就能解决，不代表硬盘要坏。",
                "warn",
            )
        else:
            add(
                "接口 CRC 错误",
                _fmt("crc_errors", crc),
                f"历史上出现过传输校验失败{since}，最近没有继续增加。建议顺手检查一下线缆。",
                "info",
            )

    # 4) 寿命 / 备用块 -----------------------------------------------------
    life_used = _num(current, "life_used")
    if life_used is None:
        left = _num(current, "ssd_life_left")
        life_used = 100 - left if left is not None else None
    if life_used is not None:
        if life_used >= LIFE_USED_WARN:
            add(
                "寿命消耗",
                f"{int(life_used)}%",
                "已经用掉九成以上标称写入寿命，剩余寿命不多，适合尽早安排替换并把数据迁走。",
                "warn",
            )
        elif life_used >= LIFE_USED_INFO:
            add(
                "寿命消耗",
                f"{int(life_used)}%",
                "写入寿命已经用掉大半，健康度正常但不再年轻，重要数据请保持双份备份。",
                "info",
            )

    spare = _num(current, "available_spare")
    if spare is not None and spare < SPARE_WARN:
        add(
            "可用备用块",
            f"{int(spare)}%",
            "备用块所剩不多，坏块继续出现时可能无处顶替，写入性能也会下降。",
            "warn",
        )

    # 5) 温度 -------------------------------------------------------------
    temp = _num(current, "temperature")
    if temp is not None:
        if temp >= TEMPERATURE_WARN:
            add(
                "工作温度",
                f"{int(temp)} °C",
                "持续高于 55 °C 会加速电子元件和闪存老化，建议改善风道、清理灰尘或加装散热片。",
                "warn",
            )
        elif temp >= TEMPERATURE_INFO:
            add(
                "工作温度",
                f"{int(temp)} °C",
                "略高于理想区间但在常见范围内，先观察长时间运行后是否继续升高。",
                "info",
            )

    # 6) 结论 -------------------------------------------------------------
    level = max(levels, key=lambda item: LEVEL_ORDER.get(item, 0)) if levels else "ok"
    headline, action = _verdict(level, is_ssd=is_ssd)

    report = Report(level=level, headline=headline, action=action, basis=basis)
    report.trends = _trend_lines(history, current, previous)
    report.advice = _advice(level, basis)
    report.meta = _meta(history, raw, model)
    return report


def _verdict(level: str, is_ssd: bool | None = None) -> tuple[str, str]:
    if level == "crit":
        return (
            "这块硬盘已经出现数据损坏风险，不建议继续存放唯一副本。",
            "先把重要数据备份到另一块盘，备份完成前不要在这块盘上做整理、扩容等写入操作，再考虑更换。",
        )
    if level == "warn":
        return (
            "这块硬盘出现需要留意的迹象，目前还能正常使用。",
            "建议一周内补一份备份，并缩短检测间隔（比如每周一次），看相关计数是否继续增长。",
        )
    if level == "info":
        return (
            "这块硬盘整体健康，只有少量提示信息，不影响使用。",
            "无需处理，保持现在的备份习惯即可。",
        )
    return (
        "这块硬盘目前没有发现任何影响数据安全的迹象。",
        "按现在的节奏使用即可，建议每月检测一次，数据保持双份备份。",
    )


def _trend_lines(
    history: list[dict[str, Any]],
    current: dict[str, Any],
    previous: dict[str, Any] | None,
) -> list[str]:
    out: list[str] = []

    if len(history) < 2:
        out.append("目前只有 1 次检测记录，还看不出趋势；积累 2~3 次之后这里会给出变化情况。")
        return out

    span = _span_text(history)
    span_text = f"，跨度 {span}" if span else ""
    out.append(
        f"共 {len(history)} 次检测，从 {_nice_date(history[0])} 到 {_nice_date(history[-1])}{span_text}。"
    )

    # 温度
    temps = [t for t in (_num(row, "temperature") for row in history) if t is not None]
    if len(temps) >= 2:
        low, high = min(temps), max(temps)
        if high - low < 3:
            out.append(f"温度很稳定，全部记录都在 {int(low)}~{int(high)} °C 之间。")
        elif temps[-1] > sum(temps[:-1]) / len(temps[:-1]) + 3:
            out.append(
                f"温度在 {int(low)}~{int(high)} °C 之间，最近一次 {int(temps[-1])} °C 高于以往平均，留意散热。"
            )
        else:
            out.append(f"温度在 {int(low)}~{int(high)} °C 之间波动，属于正常范围。")

    # 坏道类计数
    bad_keys = [k for k in BAD_SECTOR_KEYS if any((_num(r, k) or 0) > 0 for r in history)]
    if not bad_keys:
        out.append("所有记录里，重映射 / 待映射 / 不可校正扇区都保持为 0，没有发现坏道增长的迹象。")
    else:
        for key in bad_keys:
            values = [_num(r, key) or 0 for r in history]
            first_row = _first_nonzero(history, key)
            first_text = (
                f"自 {_nice_date(first_row)} 首次出现以来"
                if first_row
                else "从首次记录起"
            )
            label = BAD_SECTOR_KEYS[key]
            if values[-1] == values[0]:
                out.append(f"{label}{first_text}一直是 {int(values[-1])}，没有再增加。")
            else:
                out.append(
                    f"{label}{first_text}从 {int(values[0])} 增加到 {int(values[-1])}，仍在恶化。"
                )

    # 通电时间
    hours_now = _num(current, "power_on_hours")
    hours_prev = _num(previous, "power_on_hours")
    if hours_now is not None and hours_prev is not None:
        delta = hours_now - hours_prev
        if delta > 0:
            out.append(
                f"通电时间从上次的 {int(hours_prev):,} 小时增加到 {int(hours_now):,} 小时"
                f"（两次检测之间增加了 {int(delta)} 小时）。"
            )

    # 写入量
    lba_now = _num(current, "lba_written")
    lba_prev = _num(previous, "lba_written")
    if lba_now is not None and lba_prev is not None and lba_now > lba_prev:
        written = (lba_now - lba_prev) * 512
        out.append(
            f"两次检测之间写入了约 {_fmt_bytes(written)}，累计写入 {_fmt_bytes(lba_now * 512)}。"
        )

    # 寿命消耗
    life_now = _num(current, "life_used")
    life_prev = _num(previous, "life_used")
    if life_now is not None and life_prev is not None and life_now > life_prev:
        out.append(f"寿命消耗从 {int(life_prev)}% 上升到 {int(life_now)}%，磨损在正常推进。")

    return out


def _advice(level: str, basis: list[Basis]) -> list[str]:
    labels = {b.label for b in basis}
    out: list[str] = []

    if level == "crit":
        out.append("立刻在另一块硬盘上保留一份完整备份，备份完成前不要动这块盘上的数据。")
        out.append("对重要文件做一次抽查：打开照片、文档确认能正常读取，而不只是看文件是否存在。")
        out.append("继续每次开机后检测一次，若坏道或不可校正扇区仍在增加，尽快更换硬盘。")
    elif level == "warn":
        out.append("一周内把重要数据再备份一份，并核对关键文件能正常打开。")
        out.append("把检测间隔缩短到每周一次，重点看对应计数是否继续增长。")
    else:
        out.append("保持每月检测一次的频率，数据保持双份备份即可。")

    if "接口 CRC 错误" in labels:
        out.append("换一根新的 SATA 数据线并重新插拔两端接口，接头氧化也会导致 CRC 错误。")
    if "工作温度" in labels:
        out.append("改善散热：清理机箱灰尘、补齐风道，必要时给硬盘加装散热片。")
    if "寿命消耗" in labels or "可用备用块" in labels:
        out.append("开始规划替换：把这块盘降级为临时盘，别再存放唯一副本。")
    return out


def _meta(history: list[dict[str, Any]], raw: dict[str, Any] | None, model: str) -> list[str]:
    parts: list[str] = []
    if model:
        parts.append(f"对象：{model}")
    parts.append(f"基于 {len(history)} 次 SMART 快照")
    if len(history) >= 2:
        span = _span_text(history)
        if span:
            parts.append(f"时间跨度 {span}")
    written = normalize.host_written_bytes(raw) if raw else None
    if written:
        parts.append(f"累计主机写入 {_fmt_bytes(written)}")
    parts.append("结论由本程序根据指标与趋势规则生成，未使用大模型")
    return parts
