"""文件功能页签共用的界面组件：统计卡、后台任务线程、大小格式化。"""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from .. import verify
from . import theme


def fmt_size(size: int) -> str:
    """字节数转人话。负数/未知显示 —。"""
    if size is None or size < 0:
        return "—"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit in ("B", "KB"):
                return f"{value:,.0f} {unit}"
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"


class CountCard(QFrame):
    """统计卡：小标题在上、大数字在下，左对齐（与健康页 StatCard 同一款式）。"""

    def __init__(self, title: str, hint: str = "") -> None:
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(3)
        self.value_label = QLabel("—")
        self.value_label.setObjectName("CardValue")
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        layout.addWidget(self.value_label)
        layout.addWidget(title_label)
        if hint:
            self.setToolTip(f"{title}：{hint}")

    def set_value(self, text: str, color: str | None = None) -> None:
        self.value_label.setText(text)
        # 只覆盖颜色，字号/字重仍由 QSS 的 CardValue 提供
        self.value_label.setStyleSheet(f"color:{color or theme.TEXT};")


class TaskWorker(QThread):
    """把可暂停的长任务放到子线程：进度(dict)信号 + 协作式暂停。"""

    succeeded = Signal(object)
    failed = Signal(str)
    paused = Signal()
    progress_data = Signal(object)

    def __init__(self, task: Callable[[Any, Any], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._task = task
        self.control = verify.Control()

    def run(self) -> None:  # noqa: D102
        try:
            result = self._task(self.control, self.progress_data.emit)
            self.succeeded.emit(result)
        except verify.Paused:
            self.paused.emit()
        except Exception as exc:  # noqa: BLE001
            # 校验/扫描失败原因五花八门（硬盘掉线、权限、路径），
            # 统一带类型名抛给界面，避免只看到一句没头没尾的文字。
            self.failed.emit(f"{type(exc).__name__}: {exc}" if not str(exc) else str(exc))
