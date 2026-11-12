"""校验历史页签（P3）：把单盘自检和盘对校验的运行记录放在一起看。

回答的是"这块盘过去几次复检都什么结果""上次发现的那几个坏文件还在不在"。
明细不另存一份——每次运行的清单都在库里，选中时现场重放比对，
所以历史记录不会随数据库增长而多占空间。
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import difftree, pairing, verify, watch
from ..pairing import PairStore
from . import theme
from .common import fmt_size
from .diff_trees import DiffTrees, TreePanel

_STATUS_TEXT = {
    "finished": "完成",
    "paused": "已中断",
    "running": "进行中",
}


class HistoryTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.store = PairStore()
        self.watch = watch.WatchStore(self.store)
        self._records: list[dict[str, Any]] = []
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("校验历史")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(["全部记录", "只看单盘自检", "只看盘对校验"])
        self.filter_combo.currentIndexChanged.connect(self._render_list)
        top.addWidget(self.filter_combo)
        top.addStretch(1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        top.addWidget(self.btn_refresh)
        layout.addLayout(top)

        hint = QLabel(
            "每次结构比对、内容校验、单盘复检都会在这里留一条记录。"
            "选中某一条，下方就是那一次的差异明细。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        self.run_table = QTableWidget(0, 7)
        self.run_table.setHorizontalHeaderLabels(
            ["时间", "类型", "对象", "状态", "结论", "文件数", "耗时"]
        )
        self.run_table.verticalHeader().setVisible(False)
        self.run_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.run_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.run_table.setSelectionMode(QTableWidget.SingleSelection)
        self.run_table.setAlternatingRowColors(True)
        self.run_table.setShowGrid(False)
        header = self.run_table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for column in (0, 1, 3, 4, 5, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.run_table.itemSelectionChanged.connect(self._on_selected)
        splitter.addWidget(self.run_table)

        detail_box = QWidget()
        detail_layout = QVBoxLayout(detail_box)
        detail_layout.setContentsMargins(0, 6, 0, 0)
        detail_layout.setSpacing(6)
        self.detail_label = QLabel("选中上面一条记录查看明细")
        self.detail_label.setObjectName("Muted")
        detail_layout.addWidget(self.detail_label)

        # 明细用树展示：盘对记录用左右双树，单盘记录用"当前 vs 基线"单树
        self.detail_stack = QStackedWidget()
        self.pair_view = DiffTrees()
        self.watch_view = TreePanel(
            hint="选中一个文件，这里显示当前与基线的大小、时间、指纹和处置建议。",
        )
        self.detail_stack.addWidget(self.pair_view)    # index 0: 盘对
        self.detail_stack.addWidget(self.watch_view)   # index 1: 单盘自检
        detail_layout.addWidget(self.detail_stack, 1)
        splitter.addWidget(detail_box)

        splitter.setSizes([320, 320])
        layout.addWidget(splitter, 1)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        records = (
            watch.all_runs(self.store, 300)
            + verify.all_runs(self.store, 300)
            + self.store.all_runs(300)   # 结构比对（runs 表）
        )
        records.sort(key=lambda r: r.get("finished") or r.get("started") or "", reverse=True)
        self._records = records
        self._render_list()

    def _render_list(self) -> None:
        wanted = self.filter_combo.currentIndex()
        records = [
            r for r in self._records
            if wanted == 0
            or (wanted == 1 and r["source"] == "watch")
            or (wanted == 2 and r["source"] == "pair")
        ]

        self.run_table.setRowCount(len(records))
        for row, record in enumerate(records):
            when = record.get("finished") or record.get("started") or ""
            self.run_table.setItem(row, 0, QTableWidgetItem(when))
            kind = record.get("kind", "")
            title = record.get("title", "")
            if record["source"] == "watch":
                type_text = "单盘自检"
                if kind == "baseline":
                    title = f"{title}（建立基线）"
            elif kind == "compare":
                type_text = "结构比对"
            else:
                type_text = "内容校验"
            self.run_table.setItem(row, 1, QTableWidgetItem(type_text))
            self.run_table.setItem(row, 2, QTableWidgetItem(title))

            status_item = QTableWidgetItem(_STATUS_TEXT.get(record.get("status", ""), ""))
            if record.get("status") == "paused":
                status_item.setForeground(QColor(theme.WARN))
            self.run_table.setItem(row, 3, status_item)

            verdict, color = _verdict(record)
            verdict_item = QTableWidgetItem(verdict)
            if color:
                verdict_item.setForeground(QColor(color))
            self.run_table.setItem(row, 4, verdict_item)

            files = QTableWidgetItem(f"{record.get('files', 0):,}")
            files.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.run_table.setItem(row, 5, files)

            seconds = record.get("seconds") or 0
            self.run_table.setItem(row, 6, QTableWidgetItem(_fmt_duration(seconds)))

        if records:
            self.run_table.selectRow(0)
        else:
            self.detail_label.setText("还没有任何校验记录。到「盘对校验」或「单盘自检」跑一次就有了。")
            self.pair_view.clear()
            self.watch_view.clear()

    def _on_selected(self) -> None:
        rows = {index.row() for index in self.run_table.selectedItems()}
        if not rows:
            return
        record = self._record_at(min(rows))
        if record is None:
            return
        self._render_detail(record)

    def _record_at(self, row: int) -> dict[str, Any] | None:
        item = self.run_table.item(row, 0)
        if item is None:
            return None
        # 记录顺序与表格一致，用行号反查（同一时刻可能有多条，用对象身份不可靠）
        wanted = self.filter_combo.currentIndex()
        records = [
            r for r in self._records
            if wanted == 0
            or (wanted == 1 and r["source"] == "watch")
            or (wanted == 2 and r["source"] == "pair")
        ]
        return records[row] if 0 <= row < len(records) else None

    def _render_detail(self, record: dict[str, Any]) -> None:
        # 明细不另存：每次运行的完整清单都在库里，选中时现场重放成树
        if record["source"] == "watch":
            self.detail_stack.setCurrentIndex(1)
            try:
                root = difftree.load_watch(self.watch, record["run_id"])
            except Exception as exc:  # noqa: BLE001
                self.detail_label.setText(f"读取明细失败：{exc}")
                self.watch_view.clear()
                return
            self.watch_view.set_tree(
                root, f"自检目标 · {record.get('title', '')}", "复检"
            )
        else:
            self.detail_stack.setCurrentIndex(0)
            try:
                if record.get("kind") == "compare":
                    root = difftree.load_compare(self.store, record["run_id"])
                    source = "结构比对"
                else:
                    root = difftree.load_verify(self.store, record["run_id"])
                    source = "内容校验"
            except Exception as exc:  # noqa: BLE001
                self.detail_label.setText(f"读取明细失败：{exc}")
                self.pair_view.clear()
                return
            path_a, path_b = self._pair_paths(record.get("pair_id"))
            self.pair_view.set_tree(root, path_a, path_b, source)

        if record.get("status") == "paused":
            self.detail_label.setText(
                "这次校验中途停止了，没有产生比对结论。点【开始复检】或【内容校验】会继续。"
            )
        else:
            counts = record.get("counts") or {}
            verdict, _ = _verdict(record)
            files = root.total_files if root is not None else 0
            self.detail_label.setText(f"{verdict} · 目录树共 {files:,} 个文件")

    def _pair_paths(self, pair_id: Any) -> tuple[str, str]:
        for pair in self.store.pairs():
            if pair.id == pair_id:
                return pair.path_a, pair.path_b
        return "", ""


def _verdict(record: dict[str, Any]) -> tuple[str, str | None]:
    """一句话结论 + 颜色。"""
    counts = record.get("counts") or {}
    if record.get("status") == "paused":
        return "中途停止", theme.WARN
    if record["source"] == "watch" and record.get("kind") == "baseline":
        return "建立基线", theme.TEXT_MUTED

    bad = []
    if counts.get("corrupt"):
        bad.append(("内容损坏", counts["corrupt"], theme.CRIT))
    if counts.get("content_diff"):
        bad.append(("内容不同", counts["content_diff"], theme.CRIT))
    if counts.get("missing"):
        bad.append(("丢失", counts["missing"], theme.WARN))
    if counts.get("only_a"):
        bad.append(("只在A", counts["only_a"], theme.WARN))
    if counts.get("only_b"):
        bad.append(("只在B", counts["only_b"], theme.WARN))
    if counts.get("unreadable"):
        bad.append(("无法读取", counts["unreadable"], theme.CRIT))
    if counts.get("size_diff"):
        bad.append(("大小不同", counts["size_diff"], theme.CRIT))
    if bad:
        return " · ".join(f"{name} {n:,}" for name, n, _ in bad), bad[0][2]

    soft = []
    if counts.get("new"):
        soft.append(f"新增 {counts['new']:,}")
    if counts.get("modified"):
        soft.append(f"已修改 {counts['modified']:,}")
    if counts.get("mtime_diff"):
        soft.append(f"时间不同 {counts['mtime_diff']:,}")
    if soft:
        return "无损坏（" + " · ".join(soft) + "）", theme.OK
    same = counts.get("same", 0)
    return (f"全部一致 {same:,}" if same else "无差异"), theme.OK


def _fmt_duration(seconds: float) -> str:
    if not seconds:
        return "—"
    if seconds < 1:
        return "<1 秒"
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds / 3600:.1f} 小时"
