"""单盘自检页签（P3）：一块盘与自己的历史基线比对，不需要另一块盘在线。

和"盘对校验"的分工：
- 盘对校验：两块盘同时在手，问"这两份一样吗"；
- 单盘自检：只有一块盘，问"这块盘上的东西，跟它自己上次的状态相比，
  有没有悄悄坏掉"。

后者才是发现"照片下半部分变纯色"这类静默损坏的正确姿势：
两块盘一比对只能知道"不一样"，单盘与历史比对能知道"是这块变了"。
"""
from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import difftree, watch
from ..pairing import PairError, PairStore
from . import theme
from .changes_dialog import ChangesDialog
from .common import CountCard, TaskWorker, fmt_size
from .diff_trees import TreePanel


class NewTargetDialog(QDialog):
    """新建自检目标：一个路径 + 名称。"""

    def __init__(self, parent: QWidget | None, default_name: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建自检目标")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        tip = QLabel(
            "选一块硬盘（或其中某个文件夹）作为自检对象。\n"
            "第一次复检会建立「基线」（把这个时刻每个文件的内容指纹记下来），\n"
            "之后的每次复检都与基线比，用来发现文件悄悄损坏。\n"
            "复检只读取文件内容，不会修改任何文件。"
        )
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.name_edit = QLineEdit(default_name)
        layout.addWidget(self.name_edit)

        row = QFrame()
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        caption = QLabel("路径")
        caption.setMinimumWidth(46)
        line.addWidget(caption)
        self.path_edit = QLineEdit()
        line.addWidget(self.path_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._pick)
        line.addWidget(browse)
        layout.addWidget(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("创建")
        ok.setObjectName("Primary")
        ok.clicked.connect(self._on_ok)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def _pick(self) -> None:
        picked = QFileDialog.getExistingDirectory(self, "选择文件夹", self.path_edit.text() or "C:/")
        if picked:
            self.path_edit.setText(picked)

    def _on_ok(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            self._warn("请填写要自检的路径。")
            return
        if not os.path.isdir(path):
            self._warn(f"路径无法访问：{path}\n请确认硬盘已接上。")
            return
        self.accept()

    def _warn(self, message: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("无法创建")
        box.setIcon(QMessageBox.Warning)
        box.setText(message)
        box.addButton("返回修改", QMessageBox.AcceptRole)
        box.exec()

    @property
    def target_name(self) -> str:
        return self.name_edit.text().strip()


class WatchTab(QWidget):
    # 长任务状态广播给主窗口（顶栏的"校验进行中"指示），空串表示空闲
    activity = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.store = PairStore()
        self.watch = watch.WatchStore(self.store)
        self._worker: TaskWorker | None = None
        self._busy = False
        self._activity_text = "正在复检…"
        self._activity_prefix = "单盘复检"
        self._targets: list[watch.WatchTarget] = []
        self._build_ui()
        self._reload()

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        title = QLabel("单盘自检")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        self.target_combo = QComboBox()
        self.target_combo.setMinimumWidth(170)
        self.target_combo.currentIndexChanged.connect(self._on_target_selected)
        top.addWidget(self.target_combo, 1)

        self.btn_new = QPushButton("新建自检目标")
        self.btn_new.clicked.connect(self.on_new)
        top.addWidget(self.btn_new)
        self.btn_remove = QPushButton("移除")
        self.btn_remove.setToolTip("移到回收站（保留基线与复检历史，可恢复）")
        self.btn_remove.clicked.connect(self.on_remove)
        top.addWidget(self.btn_remove)
        self.btn_reset = QPushButton("重建基线")
        self.btn_reset.setToolTip(
            "清掉当前基线，下次复检会以当时的文件状态重新建立基线。\n"
            "已经确认过旧基线里的告警、或者整批文件都换过时使用。"
        )
        self.btn_reset.clicked.connect(self.on_reset_baseline)
        top.addWidget(self.btn_reset)
        self.btn_check = QPushButton("开始复检")
        self.btn_check.setObjectName("Primary")
        self.btn_check.setToolTip(
            "逐个读取这一侧所有文件的内容，计算 SHA-256，与基线比对（只读，不改文件）。\n"
            "能发现：新增 / 丢失 / 已修改 / 内容损坏 / 无法读取。\n"
            "首次复检只建立基线；之后每次都与同一个基线比，问题不会被吞掉。"
        )
        self.btn_check.clicked.connect(self.on_check)
        top.addWidget(self.btn_check)
        self.btn_changes = QPushButton("与上次对比")
        self.btn_changes.setToolTip(
            "把最近两次复检的问题文件对比一下：哪些是这次新坏的、哪些已经修好了。"
        )
        self.btn_changes.clicked.connect(self.on_compare_with_last)
        top.addWidget(self.btn_changes)
        layout.addLayout(top)

        self.info_card = QFrame()
        self.info_card.setObjectName("Card")
        info_layout = QVBoxLayout(self.info_card)
        info_layout.setContentsMargins(16, 12, 16, 12)
        info_layout.setSpacing(6)
        self.lbl_name = QLabel("未选择自检目标")
        self.lbl_name.setStyleSheet("font-size:15px; font-weight:600;")
        self.lbl_path = QLabel("路径：—")
        self.lbl_path.setObjectName("Muted")
        self.lbl_baseline = QLabel("基线：尚未建立")
        self.lbl_baseline.setObjectName("Muted")
        self.lbl_baseline.setWordWrap(True)
        self.lbl_last = QLabel("尚未复检过")
        self.lbl_last.setObjectName("Faint")
        self.lbl_last.setWordWrap(True)
        info_layout.addWidget(self.lbl_name)
        info_layout.addWidget(self.lbl_path)
        info_layout.addWidget(self.lbl_baseline)
        info_layout.addWidget(self.lbl_last)
        layout.addWidget(self.info_card)

        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_same = CountCard("一致", "与基线内容完全相同的文件数")
        self.card_new = CountCard("新增", "基线里没有、这次扫描到的文件（正常）")
        self.card_missing = CountCard("丢失", "基线里有、这次找不到的文件（被删了或盘有问题）")
        self.card_modified = CountCard("已修改", "大小或修改时间变了（正常编辑）")
        self.card_corrupt = CountCard(
            "内容损坏",
            "大小和修改时间都没变，但内容变了——磁盘位翻转的典型特征，"
            "这类文件应当用备份覆盖",
        )
        self.card_unreadable = CountCard("无法读取", "读的时候报错，可能是坏道或被其他程序占用")
        for card in (self.card_same, self.card_new, self.card_missing,
                     self.card_modified, self.card_corrupt, self.card_unreadable):
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        self.progress_box = QFrame()
        self.progress_box.setObjectName("Card")
        progress_layout = QVBoxLayout(self.progress_box)
        progress_layout.setContentsMargins(14, 10, 14, 10)
        progress_layout.setSpacing(6)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        progress_layout.addWidget(self.progress_bar)
        progress_row = QHBoxLayout()
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("Muted")
        progress_row.addWidget(self.progress_label, 1)
        self.btn_pause = QPushButton("暂停并保存进度")
        self.btn_pause.clicked.connect(self.on_pause)
        progress_row.addWidget(self.btn_pause)
        progress_layout.addLayout(progress_row)
        self.progress_box.hide()
        layout.addWidget(self.progress_box)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Faint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # 差异明细：目录树（当前 vs 基线），问题文件标色，详情放悬停/底部
        self.diff_view = TreePanel(
            hint="选中一个文件，这里显示当前与基线的大小、时间、指纹和处置建议。",
        )
        layout.addWidget(self.diff_view, 1)

        self.empty_hint = QLabel(
            "还没有自检目标。点【新建自检目标】，选一块硬盘（或某个文件夹）。\n"
            "第一次复检会建立基线，之后就靠每次复检与基线比对来发现文件悄悄损坏。"
        )
        self.empty_hint.setObjectName("Muted")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

    # ------------------------------------------------------------------ #
    # 状态刷新
    # ------------------------------------------------------------------ #
    def _reload(self, select_id: int | None = None) -> None:
        self._targets = self.watch.targets()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for target in self._targets:
            self.target_combo.addItem(target.name, target.id)
        self.target_combo.blockSignals(False)

        index = -1
        if select_id is not None:
            index = self.target_combo.findData(select_id)
        if index < 0 and self._targets:
            index = 0
        self.target_combo.setCurrentIndex(index)
        self._refresh_detail()

        has = bool(self._targets)
        self.target_combo.setVisible(has)
        self.info_card.setVisible(has)
        self.empty_hint.setVisible(not has)
        self.diff_view.setVisible(has)
        self.btn_remove.setEnabled(has and not self._busy)
        self.btn_reset.setEnabled(has and not self._busy)
        self.btn_check.setEnabled(has and not self._busy)
        self.btn_changes.setEnabled(has and not self._busy)
        if not has:
            self._reset_cards()
            self.status_label.setText("")
            self.diff_view.clear()

    def _reset_cards(self) -> None:
        for card in (self.card_same, self.card_new, self.card_missing,
                     self.card_modified, self.card_corrupt, self.card_unreadable):
            card.set_value("—")

    def current_target(self) -> watch.WatchTarget | None:
        index = self.target_combo.currentIndex()
        if index < 0 or index >= len(self._targets):
            return None
        return self._targets[index]

    def _on_target_selected(self, _index: int) -> None:
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        target = self.current_target()
        if target is None:
            return
        self.lbl_name.setText(target.name)
        self.lbl_path.setText(f"路径：{target.path}")

        summary = self.watch.last_run(target.id)
        if target.baseline_run_id:
            if summary and summary["kind"] == "baseline":
                self.lbl_baseline.setText(
                    f"基线：已建立于 {summary['timestamp']}"
                    f"（{summary['files']:,} 个文件 · {fmt_size(summary['bytes'])}）"
                )
            elif summary:
                self.lbl_baseline.setText(
                    f"基线：已建立 · 最近一次复检 {summary['timestamp']}"
                    f"（{summary['files']:,} 个文件 · {fmt_size(summary['bytes'])}）"
                )
            else:
                self.lbl_baseline.setText("基线：已建立")
        else:
            self.lbl_baseline.setText(
                "基线：尚未建立 —— 下次【开始复检】会先给当前状态建立基线"
            )
        self._render_last(summary)
        self._show_stored(target, summary)

    def _render_last(self, summary: dict[str, Any] | None) -> None:
        if not summary:
            self.lbl_last.setText("尚未复检过")
            return
        counts = summary.get("counts", {})
        if summary["kind"] == "baseline":
            self.lbl_last.setText(
                f"上次（{summary['timestamp']}）建立了基线，尚未做过比对"
            )
            return
        pieces = [f"一致 {counts.get('same', 0):,}"]
        for key, text in (("new", "新增"), ("missing", "丢失"),
                          ("modified", "已修改"), ("corrupt", "内容损坏"),
                          ("unreadable", "无法读取")):
            if counts.get(key, 0):
                pieces.append(f"{text} {counts[key]:,}")
        verdict = "没有发现问题" if not counts.get("corrupt") and not counts.get("missing") \
            and not counts.get("unreadable") else "需要关注"
        self.lbl_last.setText(
            f"上次复检：{summary['timestamp']} · {verdict}（{' / '.join(pieces)}）"
        )

    def _show_stored(self, target: watch.WatchTarget, summary: dict[str, Any] | None) -> None:
        """把上一次复检的结果回填到统计卡和目录树（不重新读盘）。

        没有这一步的话，切换到某个目标时看到的是六张空卡和一棵空树，
        用户会以为"没数据"，而其实历史结果就在库里。
        """
        if not summary:
            self._reset_cards()
            self.diff_view.clear()
            self.status_label.setText("")
            return

        counts = summary.get("counts", {})
        if summary["kind"] == "check":
            clean = (
                not counts.get("missing")
                and not counts.get("corrupt")
                and not counts.get("unreadable")
            )
            self._set_cards(counts, clean)

        # 目录树：watch_files 里存着这次复检的全量清单，直接从库里装
        try:
            root = difftree.load_watch(self.watch, summary["run_id"])
        except Exception:  # noqa: BLE001 - 库损坏时不能让整个页面挂掉
            root = None
        source = "复检" if summary["kind"] == "check" else "基线"
        self.diff_view.set_tree(root, f"路径 · {target.path}", source)

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def on_new(self) -> None:
        if self._busy:
            return
        dialog = NewTargetDialog(self, f"自检目标 {len(self._targets) + 1}")
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            target = self.watch.add(dialog.target_name, dialog.path_edit.text().strip())
        except PairError as exc:
            self._warn(str(exc))
            return
        self._reload(select_id=target.id)

    def on_remove(self) -> None:
        if self._busy:
            return
        target = self.current_target()
        if target is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("移除自检目标")
        box.setIcon(QMessageBox.Question)
        box.setText(f"把「{target.name}」移到回收站？")
        box.setInformativeText(
            "基线和历次复检结果都会保留，可在底部状态栏的【回收站】里恢复。\n"
            "硬盘上的文件不会有任何改动。"
        )
        confirm = box.addButton("移到回收站", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        try:
            self.watch.remove(target.id)
        except PairError as exc:
            self._warn(str(exc))
            return
        self._reload()

    def on_compare_with_last(self) -> None:
        target = self.current_target()
        if target is None:
            return
        try:
            runs = self.watch.recent_checks(target.id, 2)
            new_root = old_root = None
            if len(runs) >= 2:
                new_root = difftree.load_watch(self.watch, runs[0]["run_id"])
                old_root = difftree.load_watch(self.watch, runs[1]["run_id"])
        except Exception as exc:  # noqa: BLE001
            self._warn(f"读取历史记录失败：{exc}")
            return

        if len(runs) < 2:
            QMessageBox.information(
                self,
                "还没有可对比的记录",
                f"这个自检目标目前只有 {len(runs)} 次复检记录（建立基线那次不算）。\n"
                "再复检一次就能看出哪些文件是新出现的问题。",
            )
            return

        changes = difftree.diff_problems(old_root, new_root)
        ChangesDialog(
            changes,
            f"本次（{runs[0]['timestamp']}）",
            f"上次（{runs[1]['timestamp']}）",
            self,
        ).exec()

    def on_reset_baseline(self) -> None:
        if self._busy:
            return
        target = self.current_target()
        if target is None:
            return
        if not target.baseline_run_id:
            self._warn("这个目标还没有基线，直接点【开始复检】就会建立。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("重建基线")
        box.setIcon(QMessageBox.Warning)
        box.setText(f"确定重建「{target.name}」的基线吗？")
        box.setInformativeText(
            "重建后，下次复检会把当时的文件状态当作新的「正常状态」。\n"
            "如果现在盘上已经有损坏的文件，重建基线等于把它们当成正常的，"
            "以后再也不会被报出来。\n\n"
            "只有在你确认过当前状态没问题（例如整批文件都换过、或刚从备份恢复完）时才重建。"
        )
        confirm = box.addButton("重建基线", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        self.watch.reset_baseline(target.id)
        # 重新读取目标对象，baseline_run_id 变了
        self._reload(select_id=target.id)
        self.status_label.setText("基线已清除，下次【开始复检】会重新建立基线。")

    def on_check(self) -> None:
        if self._busy:
            return
        target = self.current_target()
        if target is None:
            return
        if not os.path.isdir(target.path):
            self._warn(
                f"路径当前无法访问：{target.path}\n"
                "硬盘没接上？接好后重试。"
            )
            return

        first = not target.baseline_run_id
        box = QMessageBox(self)
        box.setWindowTitle("确认自检")
        box.setIcon(QMessageBox.Question)
        box.setText(
            f"即将读取「{target.name}」的全部文件内容并计算 SHA-256："
            if not first
            else f"即将为「{target.name}」建立基线（读取全部文件内容计算 SHA-256）："
        )
        box.setInformativeText(
            f"路径：{target.path}\n\n"
            "· 只读取内容，不修改任何文件\n"
            "· 每个文件都会重新读一遍：机械硬盘约 150~190 MB/s，\n"
            "  4TB 数据约 6~8 小时，建议挂机过夜\n"
            "· 中途可暂停，下次继续时已算好的部分会自动沿用\n"
            "· 复检期间请勿断开硬盘\n\n"
            + (
                "这是第一次复检：会先建立基线，本次不产生差异结论。"
                if first
                else "本次会与已建立的基线逐文件比对，报出新增 / 丢失 / 已修改 / 内容损坏。"
            )
        )
        confirm = box.addButton("开始复检", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is not confirm:
            return

        self._activity_prefix = "建立基线" if first else "单盘复检"
        self._activity_text = f"{self._activity_prefix} 0%"
        self._set_busy(True)
        self.progress_box.show()
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在扫描文件…")
        worker = TaskWorker(
            lambda control, tell: watch.verify_target(self.watch, target, control, tell), self
        )
        worker.progress_data.connect(self._on_progress)
        worker.succeeded.connect(self._on_checked)
        worker.paused.connect(self._on_paused)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def on_pause(self) -> None:
        if self._worker is not None:
            self.btn_pause.setEnabled(False)
            self.progress_label.setText("正在等待已完成文件写入，即将暂停…")
            self._worker.control.pause()

    def _on_progress(self, info: dict[str, Any]) -> None:
        if info.get("phase") == "hash":
            total = info.get("total_bytes") or 0
            done = info.get("bytes") or 0
            self.progress_bar.setValue(int((done / total if total else 0) * 1000))
            self.activity.emit(
                f"{getattr(self, '_activity_prefix', '单盘复检')}"
                f" {int((done / total if total else 0) * 100)}%"
            )
            rate = info.get("rate") or 0
            eta = info.get("eta") or 0
            if eta >= 3600:
                eta_text = f"{eta / 3600:.1f} 小时"
            elif eta > 0:
                eta_text = f"{eta / 60:.0f} 分钟"
            else:
                eta_text = "即将完成"
            self.progress_label.setText(
                f"正在读取并计算指纹 · {fmt_size(done)} / {fmt_size(total)}"
                f" · {rate / 1e6:.0f} MB/s · 剩余约 {eta_text}"
                f" · 已读 {info.get('files', 0):,} 个文件"
            )
        elif info.get("phase") == "compare":
            self.progress_label.setText("正在与基线比对…")

    def _on_checked(self, result: watch.WatchResult) -> None:
        self._set_busy(False)
        self.progress_box.hide()
        target = self.current_target()
        self._reload(select_id=target.id if target else None)
        self._render_result(result)

    def _on_paused(self) -> None:
        self._set_busy(False)
        self.progress_box.hide()
        self.status_label.setText(
            "已暂停并保存进度。下次点【开始复检】会自动沿用这次已完成的文件，只读剩余部分。"
        )

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress_box.hide()
        self.status_label.setText("")
        self._warn(message)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_check.setEnabled(not busy)
        self.btn_changes.setEnabled(not busy)
        self.btn_new.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.btn_reset.setEnabled(not busy)
        self.target_combo.setEnabled(not busy)
        self.activity.emit(self._activity_text if busy else "")

    # ------------------------------------------------------------------ #
    # 结果渲染
    # ------------------------------------------------------------------ #
    def _render_result(self, result: watch.WatchResult, historical: bool = False) -> None:
        counts = result.counts
        self._set_cards(counts, result.is_clean)

        prefix = "上次复检的结果 —— " if historical else ""
        if result.baseline_created:
            self.status_label.setText(
                f"✔ 基线已建立：{result.files:,} 个文件（{fmt_size(result.bytes)}）。"
                "以后每次复检都会与这个基线比对，用来发现文件悄悄损坏。"
            )
        elif result.has_corruption:
            self.status_label.setText(
                f"{prefix}⚠ 发现 {counts['corrupt']:,} 个文件内容损坏：大小和修改时间都没变，"
                "但内容与基线不一致。这类文件应该用备份覆盖，覆盖后重新复检。"
            )
        elif not result.is_clean:
            bits = [f"{watch.CATEGORY_TEXT[k]} {counts[k]:,}"
                    for k in ("missing", "unreadable") if counts.get(k)]
            self.status_label.setText(
                f"{prefix}发现需要关注的变化：{' · '.join(bits)}。明细见下方目录树。"
            )
        else:
            self.status_label.setText(
                f"{prefix}✔ 没有发现损坏。{counts.get('same', 0):,} 个文件与基线一致"
                + (f"，另有 {counts['new']:,} 个新增、{counts['modified']:,} 个已修改（属正常变动）。"
                   if counts.get("new") or counts.get("modified") else "。")
            )

    def _set_cards(self, counts: dict[str, int], clean: bool) -> None:
        self.card_same.set_value(f"{counts.get('same', 0):,}",
                                 theme.OK if clean else None)
        self.card_new.set_value(f"{counts.get('new', 0):,}")
        self.card_missing.set_value(f"{counts.get('missing', 0):,}",
                                    theme.WARN if counts.get("missing") else None)
        self.card_modified.set_value(f"{counts.get('modified', 0):,}")
        self.card_corrupt.set_value(f"{counts.get('corrupt', 0):,}",
                                    theme.CRIT if counts.get("corrupt") else None)
        self.card_unreadable.set_value(f"{counts.get('unreadable', 0):,}",
                                       theme.CRIT if counts.get("unreadable") else None)

    def _warn(self, message: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("提示")
        box.setIcon(QMessageBox.Warning)
        box.setText(message)
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()
