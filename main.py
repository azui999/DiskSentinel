"""DiskSentinel 入口。

用法：
    python main.py        （或直接双击 run.bat）

需要管理员权限才能读取 SMART 错误日志与自检日志；
若用普通权限启动，属性数据仍可正常读取，程序会在界面中提示。
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMessageBox

from app.paths import data_dir
from app.ui import theme
from app.ui.main_window import MainWindow


def _install_excepthook() -> None:
    """双击 bat 启动时看不到控制台，未捕获异常必须落盘并弹窗，否则就是"闪退"。"""

    def hook(exc_type, exc, tb) -> None:  # noqa: ANN001
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log = data_dir() / "logs" / "error.log"
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n{text}\n")
        except OSError:
            pass
        try:
            QMessageBox.critical(
                None,
                "程序异常",
                f"{text[-1500:]}\n\n详细信息已写入：\n{log}",
            )
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook


def main() -> int:
    _install_excepthook()
    app = QApplication(sys.argv)
    app.setApplicationName("DiskSentinel")
    app.setApplicationDisplayName("磁盘哨兵")
    app.setStyle("Fusion")

    font = QFont("Microsoft YaHei UI", 9)
    font.setStyleHint(QFont.SansSerif)
    app.setFont(font)
    app.setStyleSheet(theme.QSS)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
