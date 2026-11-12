"""差异树视图：两种展示形态共用一棵「路径并集」目录树。

* DiffTrees —— 盘对校验：左右两棵行结构一致的树 + 同步滚动 + 详情条；
* TreePanel —— 单盘自检 / 历史：单棵树（当前 vs 基线）。

设计要点（与盘对页一致）：
  * 行上只显示"文件名 + 关键问题"，大小/时间/指纹/建议放 tooltip 与底部详情；
  * 问题按严重程度着色：内容损坏/大小不同=红，缺失/只在单侧=橙，
    新增/已修改/时间不同=灰蓝；父级文件夹跟随子树中最严重的问题着色；
  * 没有问题的文件夹默认收缩，且只有在展开时才真正创建子项（大目录不卡）。
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import difftree
from ..difftree import DirNode, FileNode
from ..pairing import _fmt_ts
from . import theme
from .common import fmt_size

# 一个文件夹里有太多问题文件时不自动展开，避免一次创建上万项卡住界面
AUTO_EXPAND_LIMIT = 400

SEV_COLOR = {2: theme.CRIT, 1: theme.WARN, 0: theme.INFO}


# ---------------------------------------------------------------------- #
# 行构造（DiffTrees 与 TreePanel 共用）
# ---------------------------------------------------------------------- #
def _dir_item(node: DirNode) -> QTreeWidgetItem:
    item = QTreeWidgetItem([node.name, ""])
    item.setData(0, Qt.UserRole, ("dir", node))
    item.setData(0, Qt.UserRole + 1, False)  # 子项是否已创建
    item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
    font = item.font(0)
    font.setBold(True)
    item.setFont(0, font)
    item.setToolTip(
        0,
        f"{node.path}/\n共 {node.total_files:,} 个文件"
        + (f"，{node.total_problems:,} 个问题" if node.has_problem else ""),
    )
    if node.worst_sev >= 0:
        color = SEV_COLOR[node.worst_sev]
        item.setForeground(0, QColor(color))
        item.setForeground(1, QColor(color))
        item.setText(1, f"{node.total_problems:,} 个问题")
    else:
        item.setForeground(1, QColor(theme.TEXT_FAINT))
        item.setText(1, f"{node.total_files:,} 个文件")
    return item


def _file_item(node: FileNode, side: str, names: dict[str, str]) -> QTreeWidgetItem:
    issue = node.issue_text(side)
    item = QTreeWidgetItem([node.name, issue])
    item.setData(0, Qt.UserRole, ("file", node))
    item.setToolTip(0, _file_tooltip(node, side, names))
    sev = node.sev
    if sev >= 0:
        color = SEV_COLOR[sev]
        item.setForeground(0, QColor(color))
        item.setForeground(1, QColor(color))
        if sev >= 1:
            font = item.font(1)
            font.setBold(True)
            item.setFont(1, font)
    else:
        item.setForeground(0, QColor(theme.TEXT_MUTED))
    return item


def _file_tooltip(node: FileNode, side: str, names: dict[str, str]) -> str:
    lines = [node.path, ""]
    for s, label in (("a", names["a"]), ("b", names["b"])):
        if not node.exists(s):
            lines.append(f"{label}：缺失")
            continue
        sha = node.side_sha(s)
        bits = [fmt_size(node.side_size(s))]
        if node.side_mtime(s):
            bits.append(_fmt_ts(node.side_mtime(s)))
        if sha:
            bits.append(f"SHA {sha[:12]}")
        lines.append(f"{label}：{' · '.join(bits)}")
    text = node.issue_text(side)
    if text:
        lines.append("")
        lines.append(f"问题：{text}")
        adv = difftree.advice(node)
        if adv:
            lines.append(adv)
    return "\n".join(lines)


class _DetailBar(QFrame):
    """底部详情条：行上装不下的信息全部放这里。"""

    def __init__(self, hint: str) -> None:
        super().__init__()
        self.setObjectName("Card")
        self._hint = hint
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)
        self.lbl_path = QLabel(hint)
        self.lbl_path.setObjectName("Muted")
        self.lbl_path.setWordWrap(True)
        self.lbl_a = QLabel("")
        self.lbl_a.setWordWrap(True)
        self.lbl_b = QLabel("")
        self.lbl_b.setWordWrap(True)
        self.lbl_advice = QLabel("")
        self.lbl_advice.setObjectName("Muted")
        self.lbl_advice.setWordWrap(True)
        layout.addWidget(self.lbl_path)
        layout.addWidget(self.lbl_a)
        layout.addWidget(self.lbl_b)
        layout.addWidget(self.lbl_advice)

    def reset(self) -> None:
        self.lbl_path.setText(self._hint)
        self.lbl_a.setText("")
        self.lbl_b.setText("")
        self.lbl_advice.setText("")

    def show_dir(self, node: DirNode) -> None:
        self.lbl_path.setText(
            f"{node.path}/ · 共 {node.total_files:,} 个文件"
            + (f"，{node.total_problems:,} 个问题" if node.has_problem else "")
        )
        self.lbl_a.setText("")
        self.lbl_b.setText("")
        self.lbl_advice.setText("")

    def show_file(self, node: FileNode, names: dict[str, str]) -> None:
        color = SEV_COLOR[node.sev] if node.sev >= 0 else theme.TEXT
        self.lbl_path.setText(f"<b style='color:{color}'>{node.path}</b>")
        for label, side in ((self.lbl_a, "a"), (self.lbl_b, "b")):
            if not node.exists(side):
                label.setText(
                    f"<span style='color:{theme.TEXT_FAINT}'>{names[side]}：缺失</span>"
                )
                continue
            bits = [f"<b>{fmt_size(node.side_size(side))}</b>"]
            if node.side_mtime(side):
                bits.append(_fmt_ts(node.side_mtime(side)))
            sha = node.side_sha(side)
            if sha:
                bits.append(f"SHA-256 {sha[:12]}…")
            label.setText(f"{names[side]}：" + " · ".join(bits))

        if node.has_problem:
            self.lbl_advice.setText(
                f"<span style='color:{SEV_COLOR[max(node.sev, 1)]}; font-weight:600;'>"
                f"{difftree.CAT_TEXT.get(node.issue, node.issue)}</span> · "
                f"{difftree.advice(node)}"
            )
        else:
            self.lbl_advice.setText("两侧文件名、大小、修改时间都一致。")


def _make_tree() -> QTreeWidget:
    tree = QTreeWidget()
    tree.setColumnCount(2)
    tree.setHeaderLabels(["名称", "问题"])
    tree.setUniformRowHeights(True)   # 大目录下的关键性能开关
    tree.setAnimated(False)
    tree.setIndentation(14)
    tree.setSelectionMode(QTreeWidget.SingleSelection)
    tree.setEditTriggers(QTreeWidget.NoEditTriggers)
    tree.setAlternatingRowColors(False)
    tree.setExpandsOnDoubleClick(True)
    tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    header = tree.header()
    header.setSectionResizeMode(0, QHeaderView.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
    header.setStretchLastSection(False)
    return tree


class TreePanel(QWidget):
    """单棵差异树（单盘自检 / 历史明细）：当前 vs 基线。"""

    def __init__(
        self,
        hint: str = "选中一个文件，这里显示两侧的大小、时间、指纹和处置建议。",
        names: dict[str, str] | None = None,
        title: str = "差异明细",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._names = names or {"a": "当前", "b": "基线"}
        self._root: DirNode | None = None
        self._index: dict[str, QTreeWidgetItem] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        caption = QLabel(title)
        caption.setObjectName("SectionTitle")
        bar.addWidget(caption)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("Faint")
        bar.addWidget(self.lbl_summary)
        bar.addStretch(1)

        self.btn_expand = QPushButton("展开全部问题")
        self.btn_expand.setToolTip("把所有含问题的文件夹一次展开")
        self.btn_expand.clicked.connect(self.expand_problems)
        bar.addWidget(self.btn_expand)

        self.btn_collapse = QPushButton("全部收起")
        self.btn_collapse.setToolTip("收起所有文件夹，只看顶层结构")
        self.btn_collapse.clicked.connect(self.collapse_all)
        bar.addWidget(self.btn_collapse)
        layout.addLayout(bar)

        self.label_side = QLabel("")
        self.label_side.setObjectName("Muted")
        self.label_side.setWordWrap(True)
        self.label_side.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.label_side)

        self.tree = _make_tree()
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.currentItemChanged.connect(self._on_current)
        layout.addWidget(self.tree, 1)

        self.detail = _DetailBar(hint)
        layout.addWidget(self.detail)

    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        self._root = None
        self._index = {}
        self.tree.clear()
        self.lbl_summary.setText("")
        self.label_side.setText("")
        self.detail.reset()

    def set_tree(self, root: DirNode | None, caption: str, source: str) -> None:
        self.clear()
        self.label_side.setText(caption)
        if root is None:
            return
        self._root = root
        self._fill_top(root)

        problems = root.total_problems
        files = root.total_files
        self.lbl_summary.setText(
            f"· {source}结果 · 共 {files:,} 个文件，{problems:,} 个问题"
            if problems
            else f"· {source}结果 · 共 {files:,} 个文件，全部一致"
        )
        self.lbl_summary.setStyleSheet(
            f"color:{theme.CRIT if problems else theme.OK}; font-size:12px;"
        )
        self.expand_problems()

    def _fill_top(self, root: DirNode) -> None:
        self.tree.clear()
        self._index = {}
        for node in root.sorted_dirs():
            item = _dir_item(node)
            self.tree.addTopLevelItem(item)
            self._index[node.path] = item
        for node in root.sorted_files():
            item = _file_item(node, "a", self._names)
            self.tree.addTopLevelItem(item)
            self._index[node.path] = item

    def _ensure_children(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.UserRole + 1):
            return
        payload: Any = item.data(0, Qt.UserRole)
        if not payload or payload[0] != "dir":
            return
        node: DirNode = payload[1]
        item.setData(0, Qt.UserRole + 1, True)
        for child in node.sorted_dirs():
            sub = _dir_item(child)
            item.addChild(sub)
            self._index[child.path] = sub
        for file_node in node.sorted_files():
            item.addChild(_file_item(file_node, "a", self._names))

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        self._ensure_children(item)

    def _on_current(self, current: QTreeWidgetItem | None, _prev) -> None:
        if current is None:
            return
        payload = current.data(0, Qt.UserRole)
        if not payload:
            return
        if payload[0] == "file":
            self.detail.show_file(payload[1], self._names)
        else:
            self._ensure_children(current)
            self.detail.show_dir(payload[1])

    def expand_problems(self) -> None:
        if self._root is None:
            return
        self._expand_node(self.tree, self._root)

    def _expand_node(self, tree: QTreeWidget, node: DirNode) -> None:
        for child in node.sorted_dirs():
            if not child.has_problem:
                continue
            if child.total_problems > AUTO_EXPAND_LIMIT:
                continue
            item = self._index.get(child.path)
            if item is None:
                continue
            item.setExpanded(True)
            self._ensure_children(item)
            self._expand_node(tree, child)

    def collapse_all(self) -> None:
        self.tree.collapseAll()


class DiffTrees(QWidget):
    """左右两棵对齐的文件树 + 同步滚动 + 详情条（盘对校验用）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._root: DirNode | None = None
        self._index: dict[str, dict[str, QTreeWidgetItem]] = {"a": {}, "b": {}}
        self._syncing = False
        self._names = {"a": "A 侧", "b": "B 侧"}
        self._build_ui()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        title = QLabel("差异明细")
        title.setObjectName("SectionTitle")
        bar.addWidget(title)
        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("Faint")
        bar.addWidget(self.lbl_summary)
        bar.addStretch(1)

        self.btn_expand = QPushButton("展开全部问题")
        self.btn_expand.setToolTip("把所有含问题的文件夹一次展开")
        self.btn_expand.clicked.connect(self.expand_problems)
        bar.addWidget(self.btn_expand)

        self.btn_collapse = QPushButton("全部收起")
        self.btn_collapse.setToolTip("收起所有文件夹，只看顶层结构")
        self.btn_collapse.clicked.connect(self.collapse_all)
        bar.addWidget(self.btn_collapse)

        self.chk_sync = QCheckBox("锁定同步滚动")
        self.chk_sync.setChecked(True)
        self.chk_sync.setToolTip(
            "开启后：左右两棵树滚动、展开/收起、选中都保持一致，\n"
            "同一个文件永远停在同一行，方便上下对照。"
        )
        bar.addWidget(self.chk_sync)
        layout.addLayout(bar)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.box_a, self.tree_a, self.label_a = self._build_side()
        self.box_b, self.tree_b, self.label_b = self._build_side()
        self.splitter.addWidget(self.box_a)
        self.splitter.addWidget(self.box_b)
        self.splitter.setSizes([500, 500])
        layout.addWidget(self.splitter, 1)

        self.detail = _DetailBar(
            "在上表中选择一个文件，这里显示两侧的大小、时间、指纹和处置建议。"
        )
        layout.addWidget(self.detail)

    def _build_side(self) -> tuple[QWidget, QTreeWidget, QLabel]:
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(4)

        label = QLabel("A")
        label.setObjectName("Muted")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box_layout.addWidget(label)

        tree = _make_tree()
        tree.itemExpanded.connect(lambda item, t=tree: self._on_expanded(t, item))
        tree.itemCollapsed.connect(lambda item, t=tree: self._on_collapsed(t, item))
        tree.currentItemChanged.connect(lambda cur, prev, t=tree: self._on_current(t, cur))
        tree.verticalScrollBar().valueChanged.connect(
            lambda value, t=tree: self._on_scrolled(t, value)
        )
        box_layout.addWidget(tree, 1)
        return box, tree, label

    # ------------------------------------------------------------------ #
    # 装载
    # ------------------------------------------------------------------ #
    def clear(self) -> None:
        self._root = None
        self._index = {"a": {}, "b": {}}
        for tree in (self.tree_a, self.tree_b):
            tree.clear()
        self.lbl_summary.setText("")
        self.detail.reset()

    def set_tree(self, root: DirNode | None, path_a: str, path_b: str, source: str) -> None:
        self.clear()
        self.label_a.setText(f"A · {path_a}")
        self.label_b.setText(f"B · {path_b}")
        self.label_a.setToolTip(path_a)
        self.label_b.setToolTip(path_b)
        if root is None:
            return
        self._root = root

        for tree, side in ((self.tree_a, "a"), (self.tree_b, "b")):
            self._fill_top(tree, root, side)

        problems = root.total_problems
        files = root.total_files
        self.lbl_summary.setText(
            f"· {source}结果 · 共 {files:,} 个文件，{problems:,} 个问题"
            if problems
            else f"· {source}结果 · 共 {files:,} 个文件，全部一致"
        )
        self.lbl_summary.setStyleSheet(
            f"color:{theme.CRIT if problems else theme.OK}; font-size:12px;"
        )
        self.expand_problems()

    def _fill_top(self, tree: QTreeWidget, root: DirNode, side: str) -> None:
        tree.clear()
        self._index[side] = {}
        for node in root.sorted_dirs():
            item = _dir_item(node)
            tree.addTopLevelItem(item)
            self._index[side][node.path] = item
        for node in root.sorted_files():
            item = _file_item(node, side, self._names)
            tree.addTopLevelItem(item)
            self._index[side][node.path] = item

    def _ensure_children(self, tree: QTreeWidget, item: QTreeWidgetItem, side: str) -> None:
        if item.data(0, Qt.UserRole + 1):
            return
        payload: Any = item.data(0, Qt.UserRole)
        if not payload or payload[0] != "dir":
            return
        node: DirNode = payload[1]
        item.setData(0, Qt.UserRole + 1, True)

        for child in node.sorted_dirs():
            sub = _dir_item(child)
            item.addChild(sub)
            self._index[side][child.path] = sub
        for file_node in node.sorted_files():
            item.addChild(_file_item(file_node, side, self._names))

    def _on_expanded(self, tree: QTreeWidget, item: QTreeWidgetItem) -> None:
        side = "a" if tree is self.tree_a else "b"
        self._ensure_children(tree, item, side)
        if not self._sync_on() or self._syncing:
            return
        payload = item.data(0, Qt.UserRole)
        if not payload:
            return
        twin = self._twin(tree, payload[1].path)
        if twin is not None and not twin.isExpanded():
            self._syncing = True
            try:
                twin.setExpanded(True)   # 触发对侧的 _on_expanded，自然递归
            finally:
                self._syncing = False

    def _on_collapsed(self, tree: QTreeWidget, item: QTreeWidgetItem) -> None:
        if not self._sync_on() or self._syncing:
            return
        payload = item.data(0, Qt.UserRole)
        if not payload:
            return
        twin = self._twin(tree, payload[1].path)
        if twin is not None and twin.isExpanded():
            self._syncing = True
            try:
                twin.setExpanded(False)
            finally:
                self._syncing = False

    def _on_scrolled(self, tree: QTreeWidget, value: int) -> None:
        if not self._sync_on() or self._syncing:
            return
        other = self.tree_b if tree is self.tree_a else self.tree_a
        if other.verticalScrollBar().value() == value:
            return
        self._syncing = True
        try:
            other.verticalScrollBar().setValue(value)
        finally:
            self._syncing = False

    def _on_current(self, tree: QTreeWidget, current: QTreeWidgetItem | None) -> None:
        payload = current.data(0, Qt.UserRole) if current is not None else None
        if payload:
            if payload[0] == "file":
                self.detail.show_file(payload[1], self._names)
            else:
                self.detail.show_dir(payload[1])
        if not self._sync_on() or self._syncing or current is None or not payload:
            return
        twin = self._twin(tree, payload[1].path)
        twin_tree = twin.treeWidget() if twin is not None else None
        if twin is not None and twin_tree is not None and twin_tree is not tree:
            self._syncing = True
            try:
                twin_tree.setCurrentItem(twin)
            finally:
                self._syncing = False

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _sync_on(self) -> bool:
        return self.chk_sync.isChecked()

    def _twin(self, tree: QTreeWidget, path: str) -> QTreeWidgetItem | None:
        other_side = "b" if tree is self.tree_a else "a"
        return self._index[other_side].get(path)

    def expand_problems(self) -> None:
        """把含问题的文件夹逐层展开（问题多到离谱的目录保持收起）。"""
        if self._root is None:
            return
        for tree, side in ((self.tree_a, "a"), (self.tree_b, "b")):
            self._expand_node(tree, self._root, side)

    def _expand_node(self, tree: QTreeWidget, node: DirNode, side: str) -> None:
        for child in node.sorted_dirs():
            if not child.has_problem:
                continue
            if child.total_problems > AUTO_EXPAND_LIMIT:
                continue
            item = self._index[side].get(child.path)
            if item is None:
                continue
            item.setExpanded(True)
            self._ensure_children(tree, item, side)
            self._expand_node(tree, child, side)

    def collapse_all(self) -> None:
        for tree in (self.tree_a, self.tree_b):
            tree.collapseAll()
