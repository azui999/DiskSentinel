"""两次运行的差异变化对话框：本次新出现的问题 / 上次有但这次没有了的。

回答的是用户最关心的一句话："和上次比，又坏哪儿了？"
只列问题文件，一致的文件不进列表，避免几万个文件刷屏。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..difftree import Change
from . import theme

_SEVERITY_COLOR = {2: theme.CRIT, 1: theme.WARN, 0: theme.TEXT_MUTED}


class ChangesDialog(QDialog):
    def __init__(
        self,
        changes: list[Change],
        new_title: str,
        old_title: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("与上次对比")
        self.resize(880, 560)
        self._changes = changes
        self._new_title = new_title
        self._old_title = old_title
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        new_items = [c for c in self._changes if c.kind == "new"]
        fixed_items = [c for c in self._changes if c.kind == "fixed"]

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        if new_items:
            self.summary.setText(
                f'<span style="color:{theme.CRIT}; font-weight:600;">'
                f"本次新出现 {len(new_items):,} 个问题</span>"
                + (
                    f'，<span style="color:{theme.OK}; font-weight:600;">'
                    f"{len(fixed_items):,} 个上次的问题本次没有再出现</span>"
                    if fixed_items
                    else ""
                )
                + f'<div style="color:{theme.TEXT_MUTED}; font-size:12px; margin-top:4px;">'
                f"对比范围：{self._new_title} ↔ {self._old_title}。只列问题文件，两侧一致的文件不在列表里。</div>"
            )
        elif fixed_items:
            self.summary.setText(
                f'<span style="color:{theme.OK}; font-weight:600;">'
                f"本次没有新问题，上次的 {len(fixed_items):,} 个问题已不再出现。</span>"
                f'<div style="color:{theme.TEXT_MUTED}; font-size:12px; margin-top:4px;">'
                f"对比范围：{self._new_title} ↔ {self._old_title}。</div>"
            )
        else:
            self.summary.setText(
                f'<span style="color:{theme.OK}; font-weight:600;">两次运行之间没有变化。</span>'
                f'<div style="color:{theme.TEXT_MUTED}; font-size:12px; margin-top:4px;">'
                f"对比范围：{self._new_title} ↔ {self._old_title}。</div>"
            )

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["文件", "问题"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)
        layout.addWidget(self.tree, 1)

        if new_items:
            self._add_group(
                f"本次新出现（{len(new_items):,}）", new_items, theme.CRIT
            )
        if fixed_items:
            self._add_group(
                f"上次有、本次已没有（{len(fixed_items):,}）", fixed_items, theme.OK
            )
        if not new_items and not fixed_items:
            self.tree.hide()
            label = QLabel("没有需要处理的差异变化。")
            label.setObjectName("Muted")
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label)
        else:
            self.tree.expandAll()

        bar = QHBoxLayout()
        bar.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("Primary")
        btn_close.clicked.connect(self.accept)
        bar.addWidget(btn_close)
        layout.addLayout(bar)

    def _add_group(self, text: str, changes: list[Change], color: str) -> None:
        group = QTreeWidgetItem([text, ""])
        font = group.font(0)
        font.setBold(True)
        group.setFont(0, font)
        group.setForeground(0, QColor(color))
        group.setFlags(Qt.ItemIsEnabled)
        self.tree.addTopLevelItem(group)

        for change in changes:
            row = QTreeWidgetItem([change.path, change.issue_text])
            row.setForeground(0, QColor(_SEVERITY_COLOR.get(change.severity, theme.TEXT)))
            row.setForeground(1, QColor(_SEVERITY_COLOR.get(change.severity, theme.TEXT)))
            row.setToolTip(0, change.path)
            group.addChild(row)
        group.setExpanded(True)
