"""界面配色与样式表。克制的中性底色 + 单一品牌色点缀。

注意顺序：QSS 中同优先级的选择器靠后的生效，
所以 `QWidget` 的透明底必须写在 `QWidget#Root`、`QMainWindow` 之前。
"""
from __future__ import annotations

ACCENT = "#2f6fed"
ACCENT_DARK = "#2560d8"
TEXT = "#1f2329"
TEXT_MUTED = "#6b7280"
TEXT_FAINT = "#9aa0a6"
BG = "#f5f6f8"
CARD = "#ffffff"
BORDER = "#e6e8ec"

OK = "#16a34a"
INFO = "#64748b"
WARN = "#d97706"
CRIT = "#dc2626"

LEVEL_COLORS = {
    "ok": OK,
    "info": INFO,
    "warn": WARN,
    "crit": CRIT,
    "warning": WARN,
    "error": CRIT,
}

QSS = f"""
QWidget {{
    background: transparent;
    color: {TEXT};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}}
QLabel#AppTitle {{
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel#SectionTitle {{
    font-size: 13px;
    font-weight: 600;
    color: {TEXT};
}}
QLabel#Muted {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QLabel#Faint {{
    color: {TEXT_FAINT};
    font-size: 12px;
}}
QLabel#DiskTitle {{
    font-size: 13px;
    font-weight: 600;
}}
QLabel#DiskSub {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#CardTitle {{
    color: {TEXT_MUTED};
    font-size: 12px;
}}
QLabel#CardValue {{
    font-size: 20px;
    font-weight: 600;
}}
QFrame#Card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#Panel {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#Notice {{
    background: #fffbeb;
    border: 1px solid #fde68a;
    border-radius: 8px;
}}
QFrame#Info {{
    background: #f3f7ff;
    border: 1px solid #d9e5fb;
    border-radius: 8px;
}}
QLabel#InfoText {{
    color: #33507d;
    font-size: 12px;
}}
QLabel#DiskGroup {{
    color: {TEXT_FAINT};
    font-size: 11px;
    padding-left: 4px;
}}
QPushButton {{
    background: {CARD};
    border: 1px solid #d7dae0;
    border-radius: 6px;
    padding: 6px 16px;
    color: {TEXT};
}}
QPushButton:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton#ModeFirst, QPushButton#ModeSecond {{
    padding: 6px 22px;
    font-weight: 500;
}}
/* 顶栏连体分段开关：左键右圆角、右键左圆角，中间不画分隔线 */
QPushButton#ModeFirst {{
    border-radius: 7px 0 0 7px;
    border-right: none;
}}
QPushButton#ModeSecond {{
    border-radius: 0 7px 7px 0;
}}
QPushButton#ModeFirst:checked, QPushButton#ModeSecond:checked {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#ModeFirst:checked {{
    border-right: none;
}}
QPushButton:pressed {{
    background: #eef3ff;
}}
/* 底部状态栏的「数据目录」：蓝色文字链接，无边框 */
QPushButton#LinkButton {{
    background: transparent;
    border: none;
    color: {ACCENT};
    padding: 2px 10px;
    font-size: 12px;
}}
QPushButton#LinkButton:hover {{
    color: {ACCENT_DARK};
}}
QPushButton#LinkButton:pressed {{
    background: transparent;
    color: {ACCENT_DARK};
}}
QPushButton#Primary {{
    background: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#Primary:hover {{
    background: {ACCENT_DARK};
    border-color: {ACCENT_DARK};
    color: #ffffff;
}}
QPushButton#Primary:disabled {{
    background: #c7d6f5;
    border-color: #c7d6f5;
    color: #ffffff;
}}
QPushButton:disabled {{
    color: {TEXT_FAINT};
    border-color: {BORDER};
    background: #f2f3f5;
}}
QListWidget {{
    background: {CARD};
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: 8px;
    padding: 2px;
    margin: 2px 4px;
}}
QListWidget::item:hover {{
    background: #f7f9fc;
}}
QListWidget::item:selected {{
    background: #eef3ff;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {CARD};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 7px 16px;
    margin-right: 2px;
    border: none;
    color: {TEXT_MUTED};
}}
QTabBar::tab:hover {{
    color: {ACCENT};
}}
QTabBar::tab:selected {{
    color: {ACCENT};
    font-weight: 600;
    border-bottom: 2px solid {ACCENT};
}}
QTableWidget {{
    background: {CARD};
    border: none;
    gridline-color: #f2f3f5;
    alternate-background-color: #fafbfc;
    selection-background-color: #eef3ff;
    selection-color: {TEXT};
}}
QTableWidget::item {{
    padding: 4px 6px;
}}
QTreeWidget {{
    background: {CARD};
    border: none;
    outline: none;
}}
QTreeWidget::item {{
    padding: 3px 2px;
}}
QTreeWidget::item:hover {{
    background: #f7f9fc;
}}
QTreeWidget::item:selected {{
    background: #eef3ff;
    color: {TEXT};
}}
QHeaderView::section {{
    background: #fafbfc;
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 7px 6px;
    color: {TEXT_MUTED};
    font-weight: 500;
}}
QPlainTextEdit, QTextBrowser {{
    background: {CARD};
    border: none;
    selection-background-color: #eef3ff;
}}
QProgressBar#ScanBar {{
    background: #e6e8ec;
    border: none;
    border-radius: 2px;
}}
QProgressBar#ScanBar::chunk {{
    background: {ACCENT};
    border-radius: 2px;
}}
QComboBox {{
    background: {CARD};
    border: 1px solid #d7dae0;
    border-radius: 6px;
    padding: 4px 10px;
    min-width: 140px;
}}
QComboBox:hover {{
    border-color: {ACCENT};
}}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {BORDER};
    selection-background-color: #eef3ff;
    outline: none;
}}
QStatusBar {{
    background: #fafbfc;
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QStatusBar::item {{
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #d7dae0;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #c2c7cf;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: #d7dae0;
    border-radius: 5px;
}}
QToolTip {{
    background: #2b3038;
    color: #ffffff;
    border: none;
    padding: 4px 8px;
}}
QMainWindow, QDialog {{
    background: {BG};
}}
"""
