"""主窗口。

交互原则：不做任何自动行为——扫描、检测、保存快照全部由用户点击并二次确认。
采集在子线程执行，避免 smartctl 卡住时界面假死。
"""
from __future__ import annotations

import ctypes
import json
from datetime import datetime
from typing import Any, Callable

import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import analyzer, collector, normalize
from ..collector import CollectError, DiskDevice, Snapshot
from ..store import Store
from . import theme

pg.setConfigOptions(antialias=True, background="#ffffff", foreground="#6b7280")

BADGE = {
    "ok": ("良好", "#e8f6ee", "#16803c"),
    "warn": ("注意", "#fdf3e3", "#b45309"),
    "warning": ("注意", "#fdf3e3", "#b45309"),
    "crit": ("警告", "#fdeaea", "#b91c1c"),
    "error": ("警告", "#fdeaea", "#b91c1c"),
    "info": ("未检测", "#f1f3f5", "#6b7280"),
    "none": ("未检测", "#f1f3f5", "#6b7280"),
}


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class Worker(QThread):
    """把耗时调用放到子线程，结果通过信号回到 UI 线程。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, task: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._task = task

    def run(self) -> None:  # noqa: D102
        try:
            self.succeeded.emit(self._task())
        except CollectError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# 小部件
# --------------------------------------------------------------------------- #

class DiskItemWidget(QWidget):
    def __init__(
        self, title: str, subtitle: str, badge: str, muted: bool = False
    ) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("DiskTitle")
        if muted:
            # 历史记录（当前未接入本机）整体调灰，与在线硬盘一眼区分
            title_label.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        top.addWidget(title_label, 1)

        badge_label = QLabel(badge)
        if muted:
            badge_label.setStyleSheet(
                f"color:{theme.TEXT_FAINT}; background:transparent; border-radius:7px;"
                f"border:1px dashed #d7dae0; padding:0px 6px; font-size:11px;"
            )
        else:
            badge_label.setStyleSheet(
                f"color:{theme.TEXT_MUTED}; background:#f1f3f5; border-radius:7px;"
                "padding:1px 7px; font-size:11px;"
            )
        top.addWidget(badge_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addLayout(top)

        sub_label = QLabel(subtitle)
        sub_label.setObjectName("DiskSub")
        layout.addWidget(sub_label)


class GroupRowWidget(QWidget):
    """列表里的分组标题（不可选中，只用于分隔"已接入 / 历史记录"）。"""

    def __init__(self, text: str) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 10, 2)
        label = QLabel(text)
        label.setObjectName("DiskGroup")
        layout.addWidget(label)


class StatCard(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("CardTitle")
        self.value_label = QLabel("—")
        self.value_label.setObjectName("CardValue")
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)

    def set_value(self, text: str, color: str | None = None) -> None:
        self.value_label.setText(text)
        self.value_label.setStyleSheet(f"color:{color};" if color else "")


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DiskSentinel · 磁盘哨兵")
        self.resize(1180, 760)

        self.store = Store()
        self.devices: list[DiskDevice] = []
        self.current_device: DiskDevice | None = None
        self.current_key: str | None = None
        self._worker: Worker | None = None
        self._busy = False
        self._suppress_select = False

        self._build_ui()
        self._load_archived_only()
        self._update_status_bar()
        self._show_placeholder("点击左侧【扫描硬盘】开始，所有操作均需手动触发。")

    # ------------------------------------------------------------------ #
    # 构造界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 12, 16, 8)
        root_layout.setSpacing(10)

        root_layout.addLayout(self._build_toolbar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 820])
        root_layout.addWidget(splitter, 1)

        self.setCentralWidget(root)
        self.statusBar().showMessage("就绪")
        self._build_status_widgets()

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(10)

        title = QLabel("DISKSENTINEL")
        title.setObjectName("AppTitle")
        bar.addWidget(title)

        subtitle = QLabel("本地硬盘健康记录 · 数据保存在项目目录")
        subtitle.setObjectName("Faint")
        bar.addWidget(subtitle)
        bar.addStretch(1)

        self.btn_dir = QPushButton("数据目录")
        self.btn_dir.clicked.connect(self._open_data_dir)
        bar.addWidget(self.btn_dir)

        self.btn_scan = QPushButton("扫描硬盘")
        self.btn_scan.clicked.connect(self.on_scan)
        bar.addWidget(self.btn_scan)

        self.btn_collect = QPushButton("立即检测")
        self.btn_collect.setObjectName("Primary")
        self.btn_collect.setEnabled(False)
        self.btn_collect.clicked.connect(self.on_collect)
        bar.addWidget(self.btn_collect)
        return bar

    def _build_left_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        panel.setMinimumWidth(300)
        panel.setMaximumWidth(430)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(6)

        header = QLabel("硬盘")
        header.setObjectName("SectionTitle")
        header.setContentsMargins(6, 0, 0, 0)
        layout.addWidget(header)

        self.disk_list = QListWidget()
        self.disk_list.setSelectionMode(QListWidget.SingleSelection)
        self.disk_list.currentRowChanged.connect(self.on_disk_selected)
        layout.addWidget(self.disk_list, 1)

        self.left_hint = QLabel("尚未扫描")
        self.left_hint.setObjectName("Faint")
        self.left_hint.setContentsMargins(6, 0, 0, 0)
        layout.addWidget(self.left_hint)
        return panel

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        # 选中"历史记录"（当前未接入本机）时的引导条。放在页签之外，
        # 这样任何页签下用户都能看到"为什么看不到实时数据、下一步该做什么"。
        self.offline_notice = QFrame()
        self.offline_notice.setObjectName("Info")
        offline_layout = QHBoxLayout(self.offline_notice)
        offline_layout.setContentsMargins(12, 8, 12, 8)
        offline_layout.setSpacing(10)
        self.offline_label = QLabel("")
        self.offline_label.setObjectName("InfoText")
        self.offline_label.setWordWrap(True)
        offline_layout.addWidget(self.offline_label, 1)
        self.btn_rescan = QPushButton("重新扫描")
        self.btn_rescan.clicked.connect(self.on_scan)
        offline_layout.addWidget(self.btn_rescan, 0, Qt.AlignVCenter)
        self.offline_notice.hide()
        panel_layout.addWidget(self.offline_notice)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.tab_overview = self._build_overview_tab()
        self.tab_attrs = self._build_attrs_tab()
        self.tab_trend = self._build_trend_tab()
        self.tab_report = self._build_report_tab()
        self.tab_raw = self._build_raw_tab()

        self.tabs.addTab(self.tab_overview, "概览")
        self.tabs.addTab(self.tab_attrs, "属性表")
        self.tabs.addTab(self.tab_trend, "趋势")
        self.tabs.addTab(self.tab_report, "健康报告")
        self.tabs.addTab(self.tab_raw, "原始数据")
        panel_layout.addWidget(self.tabs, 1)
        return panel

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        self.header_card = QFrame()
        self.header_card.setObjectName("Card")
        header_layout = QHBoxLayout(self.header_card)
        header_layout.setContentsMargins(16, 14, 16, 14)
        header_layout.setSpacing(12)

        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        self.lbl_model = QLabel("未选择硬盘")
        self.lbl_model.setStyleSheet("font-size:16px; font-weight:600;")
        self.lbl_sub = QLabel("—")
        self.lbl_sub.setObjectName("Muted")
        self.lbl_sub.setWordWrap(True)
        text_box.addWidget(self.lbl_model)
        text_box.addWidget(self.lbl_sub)
        header_layout.addLayout(text_box, 1)

        # 对标 CrystalDiskInfo 的关键识别信息：固件 / 接口 / 盘符 / 主机总计写入
        grid_box = QGridLayout()
        grid_box.setHorizontalSpacing(10)
        grid_box.setVerticalSpacing(3)
        self.kv_labels: dict[str, QLabel] = {}
        for row, title in enumerate(("固件版本", "接口", "盘符", "主机总计写入")):
            name = QLabel(title)
            name.setObjectName("Faint")
            value = QLabel("—")
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid_box.addWidget(name, row, 0)
            grid_box.addWidget(value, row, 1)
            self.kv_labels[title] = value
        header_layout.addLayout(grid_box)

        self.lbl_badge = QLabel("未检测")
        self.lbl_badge.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self.lbl_badge, 0, Qt.AlignTop)
        layout.addWidget(self.header_card)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_status = StatCard("总体状态")
        self.card_hours = StatCard("通电时间")
        self.card_temp = StatCard("当前温度")
        self.card_wear = StatCard("寿命消耗")
        for card in (self.card_status, self.card_hours, self.card_temp, self.card_wear):
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        self.notice = QFrame()
        self.notice.setObjectName("Notice")
        notice_layout = QHBoxLayout(self.notice)
        notice_layout.setContentsMargins(12, 8, 12, 8)
        self.notice_label = QLabel("")
        self.notice_label.setObjectName("Muted")
        self.notice_label.setWordWrap(True)
        notice_layout.addWidget(self.notice_label)
        self.notice.hide()
        layout.addWidget(self.notice)

        table_title = QLabel("指标明细")
        table_title.setObjectName("SectionTitle")
        layout.addWidget(table_title)

        self.metrics_table = QTableWidget(0, 5)
        self.metrics_table.setHorizontalHeaderLabels(["指标", "本次", "上次", "变化", "类型"])
        self.metrics_table.verticalHeader().setVisible(False)
        self.metrics_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.metrics_table.setSelectionMode(QTableWidget.NoSelection)
        self.metrics_table.setShowGrid(False)
        self.metrics_table.setAlternatingRowColors(False)
        header = self.metrics_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.metrics_table, 1)
        return page

    def _build_attrs_tab(self) -> QWidget:
        """对标 CrystalDiskInfo 的完整属性表。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        self.attrs_hint = QLabel("")
        self.attrs_hint.setObjectName("Faint")
        bar.addWidget(self.attrs_hint)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.attrs_table = QTableWidget(0, 0)
        self.attrs_table.verticalHeader().setVisible(False)
        self.attrs_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.attrs_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.attrs_table.setAlternatingRowColors(True)
        layout.addWidget(self.attrs_table, 1)
        return page

    def _render_attrs(self, raw: dict[str, Any] | None, snapshot_id: str) -> None:
        self.attrs_table.setRowCount(0)
        self.attrs_table.setColumnCount(0)
        if not raw:
            self.attrs_hint.setText("尚无快照，点击【立即检测】后可查看完整属性表")
            return

        self.attrs_hint.setText(f"当前展示最新一次快照：{_format_snapshot_id(snapshot_id)}（鼠标悬停可查看各项说明）")
        table = (raw.get("ata_smart_attributes") or {}).get("table") or []

        if table:
            self.attrs_table.setColumnCount(7)
            self.attrs_table.setHorizontalHeaderLabels(
                ["ID", "属性名称", "当前值", "最差值", "临界值", "原始值", "类型"]
            )
            rows = sorted(table, key=lambda a: a.get("id", 0))
            self.attrs_table.setRowCount(len(rows))
            for row, item in enumerate(rows):
                attr_id = item.get("id")
                name = item.get("name") or "(厂商自定义)"
                flags = item.get("flags") or {}
                attr_type = "Pre-fail" if flags.get("prefail") else "Old-age"
                when_failed = item.get("when_failed") or ""
                thresh = item.get("thresh")
                raw_block = item.get("raw") or {}

                values = [
                    str(attr_id),
                    name,
                    str(item.get("value", "—")),
                    str(item.get("worst", "—")),
                    str(thresh) if isinstance(thresh, int) else "—",
                    str(raw_block.get("string") or raw_block.get("value", "—")),
                    attr_type,
                ]
                tooltip = normalize.ata_attr_info(attr_id) if isinstance(attr_id, int) else ""
                if not tooltip:
                    tooltip = f"{name}：厂商自定义属性，含义由厂商定义，观察其变化趋势即可。"
                if when_failed and when_failed != "-":
                    tooltip += f"（当前状态：{when_failed}）"

                for col, text in enumerate(values):
                    cell = QTableWidgetItem(text)
                    if col in (0, 2, 3, 4, 5, 6):
                        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    if col == 5 and isinstance(raw_block.get("value"), int):
                        cell.setToolTip(f"HEX：{raw_block['value'] & 0xFFFFFFFFFFFF:012X}")
                    elif tooltip:
                        cell.setToolTip(tooltip)
                    self.attrs_table.setItem(row, col, cell)
        else:
            log = raw.get("nvme_smart_health_information_log") or {}
            self.attrs_table.setColumnCount(3)
            self.attrs_table.setHorizontalHeaderLabels(["属性", "当前值", "说明"])
            rows = list(log.items())
            self.attrs_table.setRowCount(len(rows))
            for row, (field, value) in enumerate(rows):
                if field == "nsid":
                    continue
                value_text = (
                    f"{value} °C" if field == "temperature" else str(value)
                )
                cells = [field, value_text, normalize.NVME_INFO.get(field, "")]
                for col, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    if col == 1:
                        cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.attrs_table.setItem(row, col, cell)

        header = self.attrs_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for col in range(2, self.attrs_table.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)

    def _build_trend_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel("指标"))
        self.metric_combo = QComboBox()
        self.metric_combo.currentIndexChanged.connect(self._redraw_trend)
        bar.addWidget(self.metric_combo)
        bar.addStretch(1)
        self.trend_hint = QLabel("")
        self.trend_hint.setObjectName("Faint")
        bar.addWidget(self.trend_hint)
        layout.addLayout(bar)

        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.plot.showGrid(x=False, y=True, alpha=0.15)
        self.plot.setMouseEnabled(x=True, y=False)
        self.plot.getAxis("left").setWidth(56)
        layout.addWidget(self.plot, 1)
        return page

    def _build_report_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self.report_view = QTextBrowser()
        self.report_view.setOpenExternalLinks(False)
        layout.addWidget(self.report_view, 1)
        return page

    def _build_raw_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel("快照"))
        self.snapshot_combo = QComboBox()
        self.snapshot_combo.currentIndexChanged.connect(self._redraw_raw)
        bar.addWidget(self.snapshot_combo)
        bar.addStretch(1)
        self.raw_path_label = QLabel("")
        self.raw_path_label.setObjectName("Faint")
        bar.addWidget(self.raw_path_label)
        layout.addLayout(bar)

        self.raw_view = QPlainTextEdit()
        self.raw_view.setReadOnly(True)
        self.raw_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(9)
        self.raw_view.setFont(font)
        layout.addWidget(self.raw_view, 1)
        return page

    def _build_status_widgets(self) -> None:
        self.lbl_smartctl = QLabel("")
        self.lbl_smartctl.setObjectName("Faint")
        self.statusBar().addPermanentWidget(self.lbl_smartctl)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _update_status_bar(self) -> None:
        parts: list[str] = []
        try:
            path = collector.find_smartctl()
            parts.append(f"smartctl {collector.smartctl_version()}  ·  {path}")
        except CollectError:
            parts.append("未找到 smartctl")
        if not is_admin():
            parts.append("非管理员（错误日志/自检日志可能读取不到）")
        self.lbl_smartctl.setText("    ".join(parts))

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.btn_scan.setEnabled(not busy)
        self.btn_collect.setEnabled(not busy and self.current_device is not None)
        if message:
            self.statusBar().showMessage(message)

    def _run(self, task: Callable[[], Any], on_ok: Callable[[Any], None], busy_message: str) -> None:
        self._set_busy(True, busy_message)
        worker = Worker(task, self)
        worker.succeeded.connect(lambda result: self._finish(on_ok, result))
        worker.failed.connect(self._fail)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _finish(self, on_ok: Callable[[Any], None], result: Any) -> None:
        self._set_busy(False)
        self._worker = None
        on_ok(result)

    def _fail(self, message: str) -> None:
        self._set_busy(False)
        self._worker = None
        self.statusBar().showMessage("操作失败", 5000)
        box = QMessageBox(self)
        box.setWindowTitle("操作失败")
        box.setIcon(QMessageBox.Warning)
        box.setText(message)
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()

    # ------------------------------------------------------------------ #
    # 左侧列表
    # ------------------------------------------------------------------ #
    def _load_archived_only(self) -> None:
        self._rebuild_list([], keep_identity=None)

    def _rebuild_list(self, devices: list[DiskDevice], keep_identity: str | None) -> None:
        self._suppress_select = True
        self.disk_list.clear()
        self._rows: list[tuple[DiskDevice | None, str | None]] = []

        for device in devices:
            key = self.store.find_key(device)
            badge = "已归档" if key else "新设备"
            subtitle = f"{device.capacity_text} · {device.serial or '无序列号'}"
            self._add_row(device.model or device.name, subtitle, badge, device, key)

        connected_keys = {
            self.store.find_key(device) for device in devices if self.store.find_key(device)
        }
        connected_serials = {
            device.serial.strip().upper()
            for device in devices
            if device.serial.strip()
        }
        archived: list[tuple[str, dict[str, Any]]] = []
        for key, entry in self.store.disks().items():
            if key in connected_keys:
                continue
            # 同序列号但 key 不同（历史脏数据）的条目也不再重复显示
            if str(entry.get("serial") or "").strip().upper() in connected_serials:
                continue
            archived.append((key, entry))

        if archived:
            if devices:
                # 分组说明行：把"在线"和"只是之前记录过"分开，避免用户把历史条目当成故障盘
                self._add_group_row("以下未接入本机 · 仅历史记录")
            for key, entry in archived:
                subtitle = f"{_capacity_text(entry.get('capacity_bytes', 0))} · {entry.get('serial') or '无序列号'}"
                self._add_row(
                    entry.get("model") or key, subtitle, "未接入", None, key, muted=True
                )

        if self._rows:
            online = len(devices)
            offline = len(archived)
            if online and offline:
                self.left_hint.setText(f"已接入 {online} 块 · 历史 {offline} 块")
            elif online:
                self.left_hint.setText(f"已接入 {online} 块硬盘")
            else:
                self.left_hint.setText(
                    f"未扫描到硬盘，当前显示 {offline} 块历史记录；点【扫描硬盘】重新检测"
                )
        else:
            self.left_hint.setText("尚未扫描到硬盘")

        target = 0 if self._rows else -1
        if keep_identity:
            for index, (device, key) in enumerate(self._rows):
                if (device and device.identity == keep_identity) or key == keep_identity:
                    target = index
                    break

        if target >= 0:
            self.disk_list.setCurrentRow(target)
            self._suppress_select = False
            device, key = self._rows[target]
            self._apply_selection(device, key)
        else:
            self._suppress_select = False
            self._apply_selection(None, None)

    def _add_row(
        self,
        title: str,
        subtitle: str,
        badge: str,
        device: DiskDevice | None,
        key: str | None,
        muted: bool = False,
    ) -> None:
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 54))
        self.disk_list.addItem(item)
        widget = DiskItemWidget(title, subtitle, badge, muted=muted)
        self.disk_list.setItemWidget(item, widget)
        self._rows.append((device, key))

    def _add_group_row(self, text: str) -> None:
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 30))
        item.setFlags(Qt.NoItemFlags)  # 不可选中、不可点
        self.disk_list.addItem(item)
        self.disk_list.setItemWidget(item, GroupRowWidget(text))
        self._rows.append((None, None))  # 占位，保持列表行号与 _rows 索引一致

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def on_scan(self) -> None:
        if self._busy:
            return
        self._run(collector.scan_devices, self._on_scanned, "正在扫描硬盘…")

    def _on_scanned(self, devices: list[DiskDevice]) -> None:
        self.devices = devices
        keep = None
        if self.current_device is not None:
            keep = self.current_device.identity
        elif self.current_key:
            keep = self.current_key
        self._rebuild_list(devices, keep)
        reachable = [d for d in devices if d.reachable]
        self.statusBar().showMessage(f"扫描完成，发现 {len(reachable)} 块可访问硬盘", 6000)

    def on_disk_selected(self, row: int) -> None:
        if self._suppress_select or row < 0 or row >= len(self._rows):
            return
        device, key = self._rows[row]
        self._apply_selection(device, key)

    def _apply_selection(self, device: DiskDevice | None, key: str | None) -> None:
        self.current_device = device
        self.current_key = key
        self.btn_collect.setEnabled(device is not None and not self._busy)
        if device is None:
            self.btn_collect.setToolTip(
                "该硬盘当前未接入本机，无法读取实时数据。\n"
                "请接上硬盘后点【扫描硬盘】，再选中它进行检测。"
            )
        else:
            self.btn_collect.setToolTip(
                "读取并保存一次 SMART 快照（只读取，不会修改硬盘内容）"
            )
        self._refresh_detail()

    def on_collect(self) -> None:
        device = self.current_device
        if device is None or self._busy:
            return
        if not self._confirm_collect(device):
            return
        self._run(
            lambda: self._collect_task(device),
            self._on_collected,
            f"正在读取 {device.model or device.name} 的 SMART 信息…",
        )

    def _collect_task(self, device: DiskDevice) -> tuple[str, Snapshot, dict[str, Any]]:
        snapshot = collector.read_snapshot(device)
        metrics = normalize.extract_metrics(snapshot.raw)
        key, _ = self.store.save_snapshot(snapshot, metrics)
        return key, snapshot, metrics

    def _on_collected(self, result: tuple[str, Snapshot, dict[str, Any]]) -> None:
        key, snapshot, _ = result
        self.current_key = key
        identity = snapshot.device.identity
        self._rebuild_list(self.devices, identity)
        self.statusBar().showMessage(
            f"已保存快照 {snapshot.snapshot_id} · {snapshot.device.model}", 6000
        )
        if snapshot.needs_admin:
            self.notice_label.setText(
                "本次未读取到 SMART 错误日志与自检日志：smartctl 需要管理员权限才能读取日志页。"
                "属性数据不受影响，如需完整日志请以管理员身份运行本程序。"
            )
            self.notice.show()

    def _confirm_collect(self, device: DiskDevice) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("确认检测")
        box.setIcon(QMessageBox.Question)
        box.setText("即将读取并保存一次 SMART 快照：")
        box.setInformativeText(
            f"型号：{device.model or '未知'}\n"
            f"序列号：{device.serial or '无'}\n"
            f"设备：{device.name}\n"
            f"容量：{device.capacity_text}\n\n"
            "该操作只读取数据，不会修改硬盘内容。是否继续？"
        )
        confirm = box.addButton("开始检测", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        return box.clickedButton() is confirm

    def _open_data_dir(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.root)))

    # ------------------------------------------------------------------ #
    # 详情渲染
    # ------------------------------------------------------------------ #
    def _show_placeholder(self, text: str) -> None:
        self.lbl_model.setText("未选择硬盘")
        self.lbl_sub.setText(text)
        self.offline_notice.hide()
        self._set_badge("none")
        for card in (self.card_status, self.card_hours, self.card_temp, self.card_wear):
            card.set_value("—")
        self.metrics_table.setRowCount(0)
        self.report_view.setHtml(f'<p style="color:{theme.TEXT_MUTED}">{text}</p>')
        self.raw_view.setPlainText("")
        self.metric_combo.clear()
        self.snapshot_combo.clear()
        self.plot.clear()

    def _refresh_detail(self) -> None:
        device = self.current_device
        key = self.current_key

        if device is None and key is None:
            self._show_placeholder("请选择左侧硬盘。")
            return

        entry = self.store.disks().get(key or "", {})
        history: list[dict[str, Any]] = self.store.history(key) if key else []
        current = history[-1] if history else None

        model = device.model if device else entry.get("model", "")
        serial = device.serial if device else entry.get("serial", "")
        capacity = device.capacity_text if device else _capacity_text(entry.get("capacity_bytes", 0))

        self.lbl_model.setText(model or "未知型号")
        bits = [capacity, f"序列号 {serial or '无'}"]
        if device:
            bits.append(device.protocol or device.dev_type)
            bits.append(device.name)
            if not device.reachable:
                bits.append("当前不可访问")
        else:
            bits.append("当前未接入本机")
        self.lbl_sub.setText(" · ".join(b for b in bits if b))

        self._update_offline_notice(history)

        raw = (
            self.store.load_raw(key, str(current.get("snapshot_id")))
            if key and current
            else None
        )
        self._fill_header_kv(device, entry, raw)

        if current is None:
            self._set_badge("none")
            for card in (self.card_status, self.card_hours, self.card_temp, self.card_wear):
                card.set_value("—")
            self.metrics_table.setRowCount(0)
            hint = (
                "该硬盘尚未保存任何快照。点击右上角【立即检测】开始第一次记录。"
                if device
                else "这块硬盘目前没有接入本机，也没有任何历史快照。接上后再扫描即可检测。"
            )
            self.report_view.setHtml(f'<p style="color:{theme.TEXT_MUTED}">{hint}</p>')
            self.metric_combo.clear()
            self.snapshot_combo.clear()
            self.plot.clear()
            self.raw_view.setPlainText("")
            self.raw_path_label.setText("")
            self.attrs_table.setRowCount(0)
            self.attrs_table.setColumnCount(0)
            self.attrs_hint.setText("尚无快照，点击【立即检测】后可查看完整属性表")
            return

        self._render_overview(current, history)
        self._render_attrs(raw, str(current.get("snapshot_id", "")))
        self._render_report(history)
        self._fill_metric_combo(history)
        self._fill_snapshot_combo(key or "")

    def _update_offline_notice(self, history: list[dict[str, Any]]) -> None:
        """选中"历史记录"时，说明为什么没有实时数据、下一步该点哪里。"""
        if self.current_device is not None:
            self.offline_notice.hide()
            return

        count = len(history)
        last = str(history[-1].get("timestamp") or "").replace("T", " ")[:16]
        if last:
            detail = f"最近一次记录于 {last}，共 {count} 次"
        else:
            detail = "还没有任何历史记录"

        self.offline_label.setText(
            f"这块硬盘当前未接入本机（{detail}），上面显示的都是历史数据，不是实时状态。"
            "若要看最新情况：把硬盘接到本机 → 点右侧【重新扫描】→ 在左侧选中它 → 【立即检测】。"
        )
        self.offline_notice.show()

    def _fill_header_kv(
        self,
        device: DiskDevice | None,
        entry: dict[str, Any],
        raw: dict[str, Any] | None,
    ) -> None:
        """头部识别信息区（对标 CrystalDiskInfo 的固件/接口/盘符/主机总计写入）。"""
        firmware = device.firmware if device else entry.get("firmware", "")
        protocol = (device.protocol if device else entry.get("protocol", "")) or "—"
        if protocol == "ATA":
            protocol = "SATA (ATA)"
        letters = device.letters_text if device else ""
        written = normalize.host_written_bytes(raw) if raw else None

        self.kv_labels["固件版本"].setText(firmware or "—")
        self.kv_labels["接口"].setText(protocol)
        self.kv_labels["盘符"].setText(letters or "—")
        self.kv_labels["主机总计写入"].setText(_format_bytes(written) if written else "—")
        self.kv_labels["主机总计写入"].setToolTip(
            "开机以来累计写入的数据量，对应 CrystalDiskInfo 的\"主机总计写入\"。\n"
            "这是估算 SSD 寿命消耗最直接的参考。"
        )

    def _render_overview(self, current: dict[str, Any], history: list[dict[str, Any]]) -> None:
        severity = str(current.get("severity") or "none")
        self._set_badge(severity)

        wear = current.get("life_used")
        if wear is None and isinstance(current.get("ssd_life_left"), (int, float)):
            wear = 100 - current["ssd_life_left"]

        status_text = "良好" if severity in ("ok", "info") else ("注意" if severity == "warning" else "警告")
        status_color = BADGE.get(severity, BADGE["none"])[2]
        if severity in ("ok", "info") and isinstance(wear, (int, float)):
            status_text = f"良好 {max(0, 100 - int(wear))}%"
        self.card_status.set_value(status_text, status_color)
        self.card_status.setToolTip(
            "总体状态来自硬盘固件的自报结果（SMART healthy / failed）。\n"
            "括号里的百分比是剩余寿命估算，仅在盘支持寿命指标时显示。"
        )

        hours = current.get("power_on_hours")
        self.card_hours.set_value(
            f"{int(hours):,} 小时" if isinstance(hours, (int, float)) else "—"
        )
        self.card_hours.setToolTip(
            "累计通电工作小时数，对应 CrystalDiskInfo 的\"通电时间\"。\n"
            "机械盘按万小时计寿命；SSD 更应关注下方的写入量与磨损指标。"
        )

        temp = current.get("temperature")
        if isinstance(temp, (int, float)):
            color = theme.CRIT if temp >= 55 else (theme.WARN if temp >= 50 else None)
            self.card_temp.set_value(f"{int(temp)} °C", color)
        else:
            self.card_temp.set_value("—")
        self.card_temp.setToolTip(
            "盘体当前温度。持续超过 55°C 会加速老化，建议改善机箱风道。\n"
            "空闲时 40~50°C 属于常见范围。"
        )

        if isinstance(wear, (int, float)):
            color = theme.WARN if wear >= 80 else None
            self.card_wear.set_value(f"{int(wear)} %", color)
        else:
            self.card_wear.set_value("—")
        self.card_wear.setToolTip(
            "按写入量/磨损估算的寿命消耗（NVMe 为 percentage_used）。\n"
            "1% 表示只消耗了 1% 标称寿命，数值越低越新；\n"
            "机械盘没有寿命概念，无该指标时显示 —。"
        )

        previous = history[-2] if len(history) >= 2 else None
        keys = [k for k in normalize.METRIC_KEYS if k in current]
        self.metrics_table.setRowCount(len(keys))
        for row, key in enumerate(keys):
            definition = normalize.DEF_BY_KEY[key]
            now_value = current.get(key)
            prev_value = previous.get(key) if previous else None

            name_item = QTableWidgetItem(definition.label)
            name_item.setToolTip(normalize.info_of(key))
            self.metrics_table.setItem(row, 0, name_item)

            now_item = QTableWidgetItem(normalize.format_value(key, now_value))
            now_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            now_item.setToolTip(normalize.info_of(key))
            self.metrics_table.setItem(row, 1, now_item)

            prev_item = QTableWidgetItem(normalize.format_value(key, prev_value) if previous else "—")
            prev_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            prev_item.setForeground(QColor(theme.TEXT_MUTED))
            self.metrics_table.setItem(row, 2, prev_item)

            delta_item = QTableWidgetItem(_delta_text(key, now_value, prev_value))
            delta_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            color = _delta_color(key, now_value, prev_value)
            if color:
                delta_item.setForeground(QColor(color))
            self.metrics_table.setItem(row, 3, delta_item)

            group_item = QTableWidgetItem(_group_text(definition.group))
            group_item.setForeground(QColor(theme.TEXT_MUTED))
            group_item.setToolTip(normalize.group_info_of(definition.group))
            self.metrics_table.setItem(row, 4, group_item)

    def _set_badge(self, severity: str) -> None:
        text, background, foreground = BADGE.get(severity, BADGE["none"])
        if self.current_device is None and self.current_key:
            text = f"{text} · 历史"
        self.lbl_badge.setText(text)
        self.lbl_badge.setStyleSheet(
            f"color:{foreground}; background:{background}; border-radius:10px;"
            "padding:4px 14px; font-size:12px; font-weight:600;"
        )

    def _render_report(self, history: list[dict[str, Any]]) -> None:
        findings = analyzer.analyze(history)
        level, summary = analyzer.summarize(findings)
        color = theme.LEVEL_COLORS.get(level, theme.INFO)

        rows = []
        for finding in findings:
            item_color = theme.LEVEL_COLORS.get(finding.level, theme.INFO)
            detail = (
                f'<div style="color:{theme.TEXT_MUTED}; font-size:12px; margin-top:2px;">{finding.detail}</div>'
                if finding.detail
                else ""
            )
            rows.append(
                f'<div style="margin:0 0 12px 0;">'
                f'<span style="color:{item_color}; font-weight:600;">[{finding.level_text}]</span> '
                f'<span style="font-weight:600;">{finding.title}</span>'
                f"{detail}</div>"
            )

        current = history[-1]
        meta = [
            f"共 {len(history)} 次检测记录",
            f"本次检测时间 {current.get('timestamp', '—')}",
        ]
        html = (
            f'<div style="font-family:\'Microsoft YaHei UI\',sans-serif; color:{theme.TEXT};">'
            f'<div style="font-size:15px; font-weight:600; color:{color}; margin-bottom:4px;">{summary}</div>'
            f'<div style="color:{theme.TEXT_MUTED}; font-size:12px; margin-bottom:16px;">'
            f'{" · ".join(meta)}</div>'
            + "".join(rows)
            + "</div>"
        )
        self.report_view.setHtml(html)

    def _fill_metric_combo(self, history: list[dict[str, Any]]) -> None:
        keys = [
            key
            for key in normalize.METRIC_KEYS
            if normalize.DEF_BY_KEY[key].plot
            and any(isinstance(row.get(key), (int, float)) for row in history)
        ]
        previous_selection = self.metric_combo.currentData()
        self.metric_combo.blockSignals(True)
        self.metric_combo.clear()
        for key in keys:
            self.metric_combo.addItem(normalize.label_of(key), key)
        if previous_selection in keys:
            self.metric_combo.setCurrentIndex(keys.index(previous_selection))
        self.metric_combo.blockSignals(False)
        self._redraw_trend()

    def _fill_snapshot_combo(self, key: str) -> None:
        ids = self.store.list_snapshots(key)
        previous = self.snapshot_combo.currentData()
        self.snapshot_combo.blockSignals(True)
        self.snapshot_combo.clear()
        for snapshot_id in ids:
            self.snapshot_combo.addItem(_format_snapshot_id(snapshot_id), snapshot_id)
        if previous in ids:
            self.snapshot_combo.setCurrentIndex(ids.index(previous))
        self.snapshot_combo.blockSignals(False)
        self._redraw_raw()

    # ------------------------------------------------------------------ #
    # 图表与原始数据
    # ------------------------------------------------------------------ #
    def _redraw_trend(self) -> None:
        self.plot.clear()
        key = self.metric_combo.currentData()
        if not key or not self.current_key:
            self.trend_hint.setText("")
            return

        history = self.store.history(self.current_key)
        points: list[tuple[float, float]] = []
        for row in history:
            value = row.get(key)
            if not isinstance(value, (int, float)):
                continue
            try:
                stamp = datetime.fromisoformat(str(row.get("timestamp"))).timestamp()
            except ValueError:
                continue
            points.append((stamp, float(value)))

        if len(points) < 2:
            self.trend_hint.setText("至少需要 2 次检测才能画出趋势")
            return

        self.trend_hint.setText(f"共 {len(points)} 个数据点")
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        pen = pg.mkPen(theme.ACCENT, width=2)
        self.plot.plot(
            xs,
            ys,
            pen=pen,
            symbol="o",
            symbolSize=6,
            symbolBrush=theme.ACCENT,
            symbolPen=pg.mkPen("#ffffff", width=1),
        )
        unit = normalize.unit_of(key)
        self.plot.setLabel("left", normalize.label_of(key), units=unit or None)
        self.plot.enableAutoRange()

    def _redraw_raw(self) -> None:
        snapshot_id = self.snapshot_combo.currentData()
        if not snapshot_id or not self.current_key:
            self.raw_view.setPlainText("")
            self.raw_path_label.setText("")
            return
        raw = self.store.load_raw(self.current_key, snapshot_id)
        if raw is None:
            self.raw_view.setPlainText("原始文件已不存在。")
            return
        self.raw_view.setPlainText(json.dumps(raw, ensure_ascii=False, indent=2))
        path = self.store.disk_dir(self.current_key) / "raw" / f"{snapshot_id}.json"
        self.raw_path_label.setText(str(path))


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _capacity_text(value: Any) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "未知容量"
    if size <= 0:
        return "未知容量"
    gb = size / 1_000_000_000
    return f"{gb:.0f} GB" if gb < 1000 else f"{gb / 1000:.2f} TB"


def _delta_text(key: str, now: Any, before: Any) -> str:
    if before is None or now is None:
        return "—"
    if not isinstance(now, (int, float)) or not isinstance(before, (int, float)):
        return "—"
    diff = now - before
    if diff == 0:
        return "0"
    sign = "+" if diff > 0 else "−"
    return f"{sign}{normalize.format_value(key, abs(diff))}"


def _delta_color(key: str, now: Any, before: Any) -> str | None:
    if before is None or not isinstance(now, (int, float)) or not isinstance(before, (int, float)):
        return None
    diff = now - before
    if diff == 0:
        return theme.TEXT_FAINT
    group = normalize.group_of(key)
    if diff > 0:
        if group in (normalize.GROUP_CRITICAL, normalize.GROUP_INTERFACE):
            return theme.CRIT
        if key == "temperature":
            return theme.WARN
        return theme.TEXT
    return theme.OK if group == normalize.GROUP_CRITICAL else theme.TEXT_MUTED


def _group_text(group: str) -> str:
    return {
        normalize.GROUP_CRITICAL: "关键",
        normalize.GROUP_INTERFACE: "接口",
        normalize.GROUP_WEAR: "磨损",
        normalize.GROUP_RUNTIME: "运行",
    }.get(group, group)


def _format_bytes(size: int | None) -> str:
    if not size:
        return "—"
    gb = size / 1_000_000_000
    if gb < 1:
        return f"{size / 1_000_000:,.0f} MB"
    return f"{gb:,.0f} GB" if gb < 1000 else f"{gb / 1000:.2f} TB"


def _format_snapshot_id(snapshot_id: str) -> str:
    try:
        stamp = datetime.strptime(snapshot_id, "%Y%m%dT%H%M%S")
    except ValueError:
        return snapshot_id
    return stamp.strftime("%Y-%m-%d %H:%M:%S")
