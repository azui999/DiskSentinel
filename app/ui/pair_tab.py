"""冷备对页签：盘对管理 + 结构比对（快通道）+ 内容校验（慢通道，P2）。

交互原则与主窗口一致：全部手动触发、操作前确认、只读不写被扫描目录。
"""
from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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

from .. import difftree, pairing, verify
from ..pairing import CompareResult, Pair, PairStore
from ..verify import VerifyResult
from . import theme
from .changes_dialog import ChangesDialog
from .common import CountCard, TaskWorker, fmt_size as _fmt_size
from .diff_trees import DiffTrees


CATEGORY_TEXT = {
    **pairing.CATEGORY_TEXT,
    **verify.CATEGORY_EXTRA,
}

VerifyWorker = TaskWorker


class NewPairDialog(QDialog):
    """新建冷备对：两个路径 + 名称。支持盘根或任意子目录。"""

    def __init__(self, parent: QWidget | None, default_name: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("新建冷备对")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        tip = QLabel(
            "选择互为冷备份的两个文件夹（整块盘选盘根，或只选要同步的子目录）。\n"
            "结构比对只读取文件名、大小和修改时间，不会读取内容或修改任何文件。"
        )
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        def path_row(label: str, edit: QLineEdit) -> QWidget:
            box = QFrame()
            row = QHBoxLayout(box)
            row.setContentsMargins(0, 0, 0, 0)
            caption = QLabel(label)
            caption.setMinimumWidth(46)
            row.addWidget(caption)
            row.addWidget(edit, 1)
            btn = QPushButton("浏览…")
            btn.clicked.connect(lambda: self._pick(edit))
            row.addWidget(btn)
            return box

        self.name_edit = QLineEdit(default_name)
        layout.addWidget(self.name_edit)
        self.a_edit = QLineEdit()
        layout.addWidget(path_row("路径 A", self.a_edit))
        self.b_edit = QLineEdit()
        layout.addWidget(path_row("路径 B", self.b_edit))

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

    def _pick(self, edit: QLineEdit) -> None:
        start = edit.text() or "C:/"
        picked = QFileDialog.getExistingDirectory(self, "选择文件夹", start)
        if picked:
            edit.setText(picked)

    def _on_ok(self) -> None:
        path_a, path_b = self.a_edit.text().strip(), self.b_edit.text().strip()
        if not path_a or not path_b:
            self._warn("两个路径都要填写。")
            return
        if not os.path.isdir(path_a):
            self._warn(f"路径 A 无法访问：{path_a}\n请确认硬盘已接上、路径正确。")
            return
        if not os.path.isdir(path_b):
            self._warn(f"路径 B 无法访问：{path_b}\n请确认硬盘已接上、路径正确。")
            return
        if os.path.abspath(path_a).lower() == os.path.abspath(path_b).lower():
            self._warn("两个路径不能是同一个文件夹。")
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
    def pair_name(self) -> str:
        return self.name_edit.text().strip()


class PairTab(QWidget):
    # 长任务状态广播给主窗口（顶栏的"校验进行中"指示），空串表示空闲
    activity = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.store = PairStore()
        self._worker: VerifyWorker | None = None
        self._busy = False
        self._activity_text = "正在处理…"
        self._pairs: list[Pair] = []
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
        title = QLabel("冷备对")
        title.setObjectName("SectionTitle")
        top.addWidget(title)
        self.pair_combo = QComboBox()
        self.pair_combo.setMinimumWidth(170)
        self.pair_combo.currentIndexChanged.connect(self._on_pair_selected)
        top.addWidget(self.pair_combo, 1)
        self.btn_new = QPushButton("新建冷备对")
        self.btn_new.clicked.connect(self.on_new)
        top.addWidget(self.btn_new)
        self.btn_remove = QPushButton("移除")
        self.btn_remove.setToolTip("移到回收站（保留比对历史，可恢复）")
        self.btn_remove.clicked.connect(self.on_remove)
        top.addWidget(self.btn_remove)
        self.btn_compare = QPushButton("结构比对")
        self.btn_compare.setToolTip(
            "扫描两侧的文件名、大小并比对（不读文件内容）。\n"
            "修改时间不参与判断——拷贝时间本来就会不同。\n"
            "能发现：缺文件 / 多文件 / 大小不同。"
        )
        self.btn_compare.clicked.connect(self.on_compare)
        top.addWidget(self.btn_compare)
        self.btn_verify = QPushButton("内容校验")
        self.btn_verify.setObjectName("Primary")
        self.btn_verify.setToolTip(
            "读取两侧每个文件的内容计算 SHA-256 并比对（只读，不修改文件）。\n"
            "这是发现静默损坏（如照片下半部分变纯色）的唯一手段。\n"
            "首次校验要读完所有文件，大目录需要较长时间；支持暂停，"
            "下次校验自动沿用已完成部分。"
        )
        self.btn_verify.clicked.connect(self.on_verify)
        top.addWidget(self.btn_verify)
        self.btn_changes = QPushButton("与上次对比")
        self.btn_changes.setToolTip(
            "把最近两次运行的问题文件对比一下：哪些是这次新坏的、哪些已经修好了。\n"
            "与页面上正在看的树用同一数据来源（优先内容校验，否则结构比对）。"
        )
        self.btn_changes.clicked.connect(self.on_compare_with_last)
        top.addWidget(self.btn_changes)
        layout.addLayout(top)

        # 信息卡
        self.info_card = QFrame()
        self.info_card.setObjectName("Card")
        info_layout = QVBoxLayout(self.info_card)
        info_layout.setContentsMargins(16, 12, 16, 12)
        info_layout.setSpacing(6)
        self.lbl_pair_name = QLabel("未选择冷备对")
        self.lbl_pair_name.setStyleSheet("font-size:15px; font-weight:600;")

        def side_badge(letter: str) -> QLabel:
            badge = QLabel(letter)
            badge.setFixedSize(22, 22)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                f"background:{theme.ACCENT_SOFT}; color:{theme.ACCENT};"
                "border-radius:6px; font-weight:600; font-size:11px;"
            )
            return badge

        self.lbl_path_a = QLabel("—")
        self.lbl_path_b = QLabel("—")
        self.lbl_path_a.setObjectName("Muted")
        self.lbl_path_b.setObjectName("Muted")
        for lbl in (self.lbl_path_a, self.lbl_path_b):
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        def path_row(letter: str, label: QLabel) -> QWidget:
            row = QFrame()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            row_layout.addWidget(side_badge(letter))
            row_layout.addWidget(label, 1)
            return row

        self.row_a = path_row("A", self.lbl_path_a)
        self.row_b = path_row("B", self.lbl_path_b)
        self.lbl_last = QLabel("尚未比对过")
        self.lbl_last.setObjectName("Faint")
        info_layout.addWidget(self.lbl_pair_name)
        info_layout.addWidget(self.row_a)
        info_layout.addWidget(self.row_b)
        info_layout.addWidget(self.lbl_last)
        layout.addWidget(self.info_card)

        # 统计卡行
        cards = QHBoxLayout()
        cards.setSpacing(10)
        self.card_same = CountCard("完全一致", "两侧文件名、大小都相同的文件数（修改时间不参与判断）")
        self.card_only_a = CountCard("只在 A", "只有 A 侧有、B 侧缺失的文件（通常是漏同步）")
        self.card_only_b = CountCard("只在 B", "只有 B 侧有、A 侧缺失的文件（通常是 B 侧多出来的）")
        self.card_size = CountCard("大小不同", "同名文件两侧大小不一样，多半是没拷完或被截断")
        self.card_content = CountCard(
            "内容不同", "大小相同、但 SHA-256 指纹不同——这是静默损坏的唯一证据"
        )
        for card in (self.card_same, self.card_only_a, self.card_only_b,
                     self.card_size, self.card_content):
            cards.addWidget(card, 1)
        layout.addLayout(cards)

        # 进度区（内容校验运行时显示）
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

        # 差异明细：左右两棵对齐的文件树（同步滚动），问题文件标红
        self.diff_view = DiffTrees()
        layout.addWidget(self.diff_view, 1)

        self.empty_hint = QLabel(
            "还没有冷备对。点【新建冷备对】，把两块互为备份的硬盘（或要同步的子目录）配成一对。"
        )
        self.empty_hint.setObjectName("Muted")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

    # ------------------------------------------------------------------ #
    # 状态刷新
    # ------------------------------------------------------------------ #
    def _reload(self, select_id: int | None = None) -> None:
        self._pairs = self.store.pairs()
        self.pair_combo.blockSignals(True)
        self.pair_combo.clear()
        for pair in self._pairs:
            self.pair_combo.addItem(pair.name, pair.id)
        self.pair_combo.blockSignals(False)

        index = -1
        if select_id is not None:
            index = self.pair_combo.findData(select_id)
        if index < 0 and self._pairs:
            index = 0
        self.pair_combo.setCurrentIndex(index)
        self._refresh_detail()

        has = bool(self._pairs)
        self.pair_combo.setVisible(has)
        self.info_card.setVisible(has)
        self.btn_remove.setEnabled(has and not self._busy)
        self.btn_compare.setEnabled(has and not self._busy)
        self.btn_verify.setEnabled(has and not self._busy)
        self.btn_changes.setEnabled(has and not self._busy)
        self.empty_hint.setVisible(not has)
        self.diff_view.setVisible(has)
        if not has:
            self._reset_cards()
            self.status_label.setText("")
            self.diff_view.clear()

    def _reset_cards(self) -> None:
        for card in (self.card_same, self.card_only_a, self.card_only_b,
                     self.card_size, self.card_content):
            card.set_value("—")

    def current_pair(self) -> Pair | None:
        index = self.pair_combo.currentIndex()
        if index < 0 or index >= len(self._pairs):
            return None
        return self._pairs[index]

    def _on_pair_selected(self, _index: int) -> None:
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        pair = self.current_pair()
        if pair is None:
            return
        self.lbl_pair_name.setText(pair.name)
        self.lbl_path_a.setText(pair.path_a)
        self.lbl_path_b.setText(pair.path_b)
        self._render_last_run()
        self._render_stored_cards(pair)
        self._load_tree(pair)

    def _render_stored_cards(self, pair: Pair) -> None:
        """把库里最近一次运行的统计回填到六张卡片。

        树和"上次运行"信息行在打开页面时都会回填，唯独卡片是空的——
        用户会以为"没数据"，其实历史结果就在库里。
        """
        last = verify.last_verify(self.store, pair.id)
        if last:
            counts = last["counts"]
            clean = not any(
                counts.get(key) for key in ("only_a", "only_b", "size_diff", "mtime_diff", "content_diff")
            )
            self._set_cards(counts, clean)
            return
        summary = self.store.last_run(pair.id)
        if summary:
            counts = summary.get("counts", {})
            clean = not any(
                counts.get(key) for key in ("only_a", "only_b", "size_diff", "mtime_diff")
            )
            self._set_cards(counts, clean)
        else:
            self._reset_cards()

    def _load_tree(self, pair: Pair) -> None:
        """把最近一次比对/校验的完整清单装进双树视图（只读 SQLite，不碰硬盘）。"""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            root, source = difftree.load_latest(self.store, pair.id)
        except Exception:  # noqa: BLE001 - 库损坏时不能让整个页面挂掉
            root, source = None, ""
        finally:
            QApplication.restoreOverrideCursor()

        if root is None:
            self.diff_view.clear()
            return
        self.diff_view.set_tree(root, pair.path_a, pair.path_b, source)

    def _render_last_run(self) -> None:
        pair = self.current_pair()
        if pair is None:
            return
        parts = []
        summary = self.store.last_run(pair.id)
        if summary:
            counts = summary.get("counts", {})
            pieces = [f"一致 {counts.get('same', 0):,}"]
            for key, text in (("only_a", "只在A"), ("only_b", "只在B"),
                              ("size_diff", "大小不同"), ("mtime_diff", "时间不同")):
                if counts.get(key, 0):
                    pieces.append(f"{text} {counts[key]:,}")
            verdict = "完全一致" if len(pieces) == 1 else "有差异"
            parts.append(f"结构比对：{summary['timestamp']} · {verdict}（{' / '.join(pieces)}）")
        last = verify.last_verify(self.store, pair.id)
        if last:
            counts = last["counts"]
            quick = "· 快速模式" if last.get("incremental") else ""
            if counts.get("content_diff"):
                parts.append(
                    f"内容校验：{last['timestamp']}{quick} · 发现 {counts['content_diff']:,} 个内容损坏嫌疑"
                )
            else:
                hours = last.get("seconds", 0) / 3600
                parts.append(
                    f"内容校验：{last['timestamp']}{quick} · 内容全部一致"
                    f"（校验 {last.get('hashed_files', 0) + last.get('reused_files', 0):,} 个文件"
                    f"，用时 {hours:.1f} 小时）"
                )
        self.lbl_last.setText(" · ".join(parts) if parts else "尚未比对过")

    # ------------------------------------------------------------------ #
    # 动作
    # ------------------------------------------------------------------ #
    def on_new(self) -> None:
        if self._busy:
            return
        default = f"冷备对 {len(self._pairs) + 1}"
        dialog = NewPairDialog(self, default)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            pair = self.store.add(dialog.pair_name, dialog.a_edit.text(), dialog.b_edit.text())
        except pairing.PairError as exc:
            self._warn(str(exc))
            return
        self._reload(select_id=pair.id)

    def on_remove(self) -> None:
        if self._busy:
            return
        pair = self.current_pair()
        if pair is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("移除冷备对")
        box.setIcon(QMessageBox.Question)
        box.setText(f"把「{pair.name}」移到回收站？")
        box.setInformativeText(
            "配对关系和比对历史都会保留，可在底部状态栏的【回收站】里恢复。\n"
            "两块硬盘上的文件不会有任何改动。"
        )
        confirm = box.addButton("移到回收站", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        try:
            self.store.remove(pair.id)
        except pairing.PairError as exc:
            self._warn(str(exc))
            return
        self._reload()

    # -- 与上次对比 ------------------------------------------------------- #
    def on_compare_with_last(self) -> None:
        pair = self.current_pair()
        if pair is None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        error = ""
        try:
            # 数据来源与页面上正在看的树保持一致
            if verify.last_verify(self.store, pair.id):
                runs = verify.recent_runs(self.store, pair.id, 2)
                source_text = "内容校验"
                load = lambda rid: difftree.load_verify(self.store, rid)  # noqa: E731
            else:
                runs = self.store.recent_runs(pair.id, 2)
                source_text = "结构比对"
                load = lambda rid: difftree.load_compare(self.store, rid)  # noqa: E731
            new_root = old_root = None
            if len(runs) >= 2:
                new_root = load(runs[0]["run_id"])
                old_root = load(runs[1]["run_id"])
        except Exception as exc:  # noqa: BLE001 - 库损坏时给提示，不崩
            error = str(exc)
            runs, source_text = [], ""
        finally:
            QApplication.restoreOverrideCursor()

        if error:
            self._warn(f"读取历史记录失败：{error}")
            return
        if len(runs) < 2:
            QMessageBox.information(
                self,
                "还没有可对比的记录",
                f"这个冷备对目前只有 {len(runs)} 次{source_text}记录。\n"
                "再跑一次之后，就能看出哪些文件是新出现的问题。",
            )
            return

        changes = difftree.diff_problems(old_root, new_root)
        ChangesDialog(
            changes,
            f"本次（{runs[0]['timestamp']}）",
            f"上次（{runs[1]['timestamp']}）",
            self,
        ).exec()

    # -- 结构比对 --------------------------------------------------------- #
    def on_compare(self) -> None:
        if self._busy:
            return
        pair = self.current_pair()
        if pair is None:
            return
        missing = [label for label, path in (("A", pair.path_a), ("B", pair.path_b))
                   if not os.path.isdir(path)]
        if missing:
            self._warn(
                f"路径 {' 和 '.join(missing)} 当前无法访问（硬盘没接上？）。\n"
                "接上后重新选择路径，或删除这个冷备对后重新创建。"
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle("确认结构比对")
        box.setIcon(QMessageBox.Question)
        box.setText(f"即将扫描并比对「{pair.name}」的两侧：")
        box.setInformativeText(
            f"A：{pair.path_a}\nB：{pair.path_b}\n\n"
            "只读取文件名、大小、修改时间，不读文件内容，不修改任何文件。\n"
            "文件数量多时需要几分钟，期间请保持硬盘连接。"
        )
        confirm = box.addButton("开始比对", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is not confirm:
            return

        self._activity_text = "结构比对中…"
        self._set_busy(True)
        self.status_label.setText("正在准备…")
        worker = VerifyWorker(
            lambda control, tell: pairing.run_compare(self.store, pair, tell), self
        )
        worker.progress_data.connect(self._on_text_progress)
        worker.succeeded.connect(self._on_compared)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_compared(self, result: tuple[CompareResult, int]) -> None:
        self._set_busy(False)
        compare, _run_id = result
        self._render_result(compare.counts, clean=compare.is_clean, clean_text=None)
        self._refresh_detail()

    # -- 内容校验 --------------------------------------------------------- #
    def on_verify(self) -> None:
        if self._busy:
            return
        pair = self.current_pair()
        if pair is None:
            return
        missing = [label for label, path in (("A", pair.path_a), ("B", pair.path_b))
                   if not os.path.isdir(path)]
        if missing:
            self._warn(
                f"路径 {' 和 '.join(missing)} 当前无法访问（硬盘没接上？）。\n"
                "接上后再开始校验。"
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle("确认内容校验")
        box.setIcon(QMessageBox.Question)
        box.setText(f"即将逐个读取「{pair.name}」两侧的全部文件内容，计算 SHA-256 并比对：")
        box.setInformativeText(
            f"A：{pair.path_a}\nB：{pair.path_b}\n\n"
            "· 只读取内容，不修改任何文件\n"
            "· 每个文件都会重新读一遍：机械硬盘约 150~190 MB/s，\n"
            "  4TB 数据约 6~8 小时（每侧），建议挂机过夜\n"
            "· 中途暂停后继续时，已算好的部分会自动沿用\n"
            "· 校验期间请勿断开硬盘、不要访问这些文件"
        )
        quick = QCheckBox("快速模式：跳过大小和修改时间都没变的文件")
        quick.setToolTip(
            "勾选后速度快很多，但无法发现静默损坏——\n"
            "文件损坏时大小和修改时间都不会变，跳过就等于没检查。\n"
            "只有确定这些文件近期没人动过、只想快速看有没有漏同步时才用。"
        )
        box.setCheckBox(quick)
        confirm = box.addButton("开始校验", QMessageBox.AcceptRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        box.exec()
        if box.clickedButton() is not confirm:
            return
        incremental = quick.isChecked()

        self._activity_text = "内容校验 0%"
        self._set_busy(True)
        self.progress_box.show()
        self.progress_bar.setValue(0)
        self.progress_label.setText("正在准备…")
        worker = VerifyWorker(
            lambda control, tell: verify.verify_pair(
                self.store, pair, control, tell, incremental=incremental
            ),
            self,
        )
        worker.progress_data.connect(self._on_verify_progress)
        worker.succeeded.connect(self._on_verified)
        worker.paused.connect(self._on_verify_paused)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def on_pause(self) -> None:
        if self._worker is not None:
            self.btn_pause.setEnabled(False)
            self.progress_label.setText("正在等待已完成文件写入，即将暂停…")
            self._worker.control.pause()

    def _on_verify_progress(self, info: dict[str, Any]) -> None:
        phase = info.get("phase")
        if phase == "hash":
            total = info.get("total_bytes") or 0
            done = info.get("bytes") or 0
            ratio = done / total if total else 0
            self.progress_bar.setValue(int(ratio * 1000))
            self.activity.emit(f"内容校验 {int(ratio * 100)}%")
            side = "A" if info.get("side") == "a" else "B"
            rate = info.get("rate") or 0
            eta = info.get("eta") or 0
            if eta >= 3600:
                eta_text = f"{eta/3600:.1f} 小时"
            elif eta > 0:
                eta_text = f"{eta/60:.0f} 分钟"
            else:
                eta_text = "即将完成"
            self.progress_label.setText(
                f"正在校验 {side} 侧 · {_fmt_size(done)} / {_fmt_size(total)}"
                f" · {rate/1e6:.0f} MB/s · 剩余约 {eta_text}"
                f" · 已沿用 {info.get('reused', 0):,} 个"
            )
        elif phase == "compare":
            self.progress_label.setText("正在比对…")

    def _on_verified(self, result: VerifyResult) -> None:
        self._set_busy(False)
        self.progress_box.hide()
        self._render_result(result.counts, clean=result.is_clean, clean_text=None)
        self._refresh_detail()
        note = ""
        if result.reused_files:
            note = f"（断点续跑沿用 {result.reused_files:,} 个 · 本次实读 {result.hashed_files:,} 个）"
        if result.incremental:
            skipped = result.reused_files
            note += f" · 快速模式跳过了 {skipped:,} 个未变动文件，这些文件本次没有被读取"
        self.status_label.setText(
            f"内容校验完成{note}。"
            if not result.is_clean
            else f"✔ 两侧所有文件内容完全一致{note}。"
        )

    def _on_verify_paused(self) -> None:
        self._set_busy(False)
        self.progress_box.hide()
        self.status_label.setText(
            "已暂停并保存进度。下次点【内容校验】会自动沿用这次已完成的文件，只读取剩余部分。"
        )

    def _on_text_progress(self, message: str) -> None:
        self.status_label.setText(message)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress_box.hide()
        self.status_label.setText("")
        box = QMessageBox(self)
        box.setWindowTitle("操作失败")
        box.setIcon(QMessageBox.Warning)
        box.setText(message)
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_compare.setEnabled(not busy)
        self.btn_verify.setEnabled(not busy)
        self.btn_changes.setEnabled(not busy)
        self.btn_new.setEnabled(not busy)
        self.btn_remove.setEnabled(not busy)
        self.pair_combo.setEnabled(not busy)
        self.activity.emit(self._activity_text if busy else "")

    # ------------------------------------------------------------------ #
    # 结果渲染
    # ------------------------------------------------------------------ #
    def _render_result(
        self,
        counts: dict[str, int],
        clean: bool,
        clean_text: str | None,
    ) -> None:
        self._set_cards(counts, clean)

        if clean:
            self.status_label.setText(
                clean_text
                or f"✔ 两侧结构完全一致（{counts.get('same', 0):,} 个文件）。"
                "注意：结构一致不等于内容完好，文件是否损坏请点【内容校验】。"
            )
        else:
            bits = [f"{text} {counts.get(key, 0):,}"
                    for key, text in CATEGORY_TEXT.items()
                    if counts.get(key)]
            same = counts.get("same", 0)
            self.status_label.setText(
                f"发现差异：{' · '.join(bits)}（其余 {same:,} 个完全一致）。"
                "差异已按目录结构列在下面：标红的文件就是问题项，"
                "鼠标悬停或点击可看两侧大小、时间和指纹。"
            )

    def _set_cards(self, counts: dict[str, int], clean: bool) -> None:
        self.card_same.set_value(f"{counts.get('same', 0):,}",
                                 theme.OK if clean else None)
        self.card_only_a.set_value(f"{counts.get('only_a', 0):,}",
                                   theme.WARN if counts.get("only_a") else None)
        self.card_only_b.set_value(f"{counts.get('only_b', 0):,}",
                                   theme.WARN if counts.get("only_b") else None)
        self.card_size.set_value(f"{counts.get('size_diff', 0):,}",
                                 theme.CRIT if counts.get("size_diff") else None)
        self.card_content.set_value(f"{counts.get('content_diff', 0):,}",
                                    theme.CRIT if counts.get("content_diff") else None)

    def _warn(self, message: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("提示")
        box.setIcon(QMessageBox.Warning)
        box.setText(message)
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()
