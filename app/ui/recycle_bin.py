"""回收站：删除的冷备对 / 自检目标先放这里，可恢复或彻底删除。

为什么不做成「真删 + 二次确认」：冷备对和自检目标背后是几百次比对历史，
误删一次就全没了。软删除只是加一个时间戳，代价接近零。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..pairing import PairError, PairStore
from ..watch import WatchStore


class RecycleBinDialog(QDialog):
    """列出已删除的对象；恢复 / 彻底删除后通过 changed 通知外层刷新。"""

    changed = Signal()

    def __init__(self, store: PairStore, watch_store: WatchStore, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("回收站")
        self.resize(760, 440)
        self.store = store
        self.watch = watch_store
        self._rows: list[tuple[str, int]] = []
        self._build_ui()
        self.reload()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        hint = QLabel(
            "删除的冷备对和自检目标会先放到这里，比对历史一并保留。"
            "确认不再需要时再「彻底删除」，那之后历史记录不可找回。"
        )
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["类型", "名称", "位置", "删除时间"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.table, 1)

        self.empty_label = QLabel("回收站是空的。")
        self.empty_label.setObjectName("Faint")
        self.empty_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_label)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_restore = QPushButton("恢复所选")
        self.btn_restore.setObjectName("Primary")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_purge = QPushButton("彻底删除")
        self.btn_purge.setToolTip("连同所有历史记录一起删除，无法恢复")
        self.btn_purge.clicked.connect(self._purge)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bar.addWidget(self.btn_restore)
        bar.addWidget(self.btn_purge)
        bar.addStretch(1)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        entries: list[tuple[str, int, str, str, str, str]] = []
        for pair, deleted_at in self.store.deleted_pairs():
            entries.append(
                ("pair", pair.id, "冷备对", pair.name,
                 f"{pair.path_a}  ⇄  {pair.path_b}", deleted_at)
            )
        for target, deleted_at in self.watch.deleted_targets():
            entries.append(("watch", target.id, "单盘自检", target.name,
                            target.path, deleted_at))
        entries.sort(key=lambda e: e[5], reverse=True)

        self._rows = [(kind, ident) for kind, ident, *_ in entries]
        self.table.setRowCount(len(entries))
        for row, (_kind, _ident, type_text, name, where, deleted_at) in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(type_text))
            self.table.setItem(row, 1, QTableWidgetItem(name))
            where_item = QTableWidgetItem(where)
            where_item.setToolTip(where)
            self.table.setItem(row, 2, where_item)
            stamp = QTableWidgetItem(deleted_at)
            stamp.setForeground(Qt.gray)
            self.table.setItem(row, 3, stamp)

        has = bool(entries)
        self.table.setVisible(has)
        self.empty_label.setVisible(not has)
        self.btn_restore.setEnabled(False)
        self.btn_purge.setEnabled(False)
        if has:
            self.table.selectRow(0)

    def _current(self) -> tuple[str, int] | None:
        rows = {index.row() for index in self.table.selectedItems()}
        if not rows:
            return None
        row = min(rows)
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _update_buttons(self) -> None:
        current = self._current()
        self.btn_restore.setEnabled(current is not None)
        self.btn_purge.setEnabled(current is not None)

    def _name_of(self, row: int) -> str:
        item = self.table.item(row, 1)
        return item.text() if item else ""

    def _restore(self) -> None:
        current = self._current()
        if current is None:
            return
        kind, ident = current
        try:
            if kind == "pair":
                self.store.restore(ident)
            else:
                self.watch.restore(ident)
        except PairError as exc:
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        self.changed.emit()
        self.reload()

    def _purge(self) -> None:
        current = self._current()
        if current is None:
            return
        rows = {index.row() for index in self.table.selectedItems()}
        name = self._name_of(min(rows)) if rows else ""
        kind, ident = current
        box = QMessageBox(self)
        box.setWindowTitle("彻底删除")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"彻底删除「{name}」？")
        box.setInformativeText(
            "这会连同它的全部比对/复检历史一起删除，之后无法恢复。\n"
            "硬盘上的文件不会受任何影响。"
        )
        confirm = box.addButton("彻底删除", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[1])
        box.exec()
        if box.clickedButton() is not confirm:
            return
        try:
            if kind == "pair":
                self.store.purge(ident)
            else:
                self.watch.purge(ident)
        except PairError as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.changed.emit()
        self.reload()
