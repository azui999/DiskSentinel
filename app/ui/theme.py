"""界面配色与样式表。克制的中性底色 + 单一品牌色点缀。

设计系统约定：
  * 间距走 8px 网格；圆角两档：卡片 10px、控件 7px；
  * 字号层级：页面标题 15-16px / 正文 13px / 辅助说明 12px / 状态栏 11px；
  * 颜色只有一套语义色：OK 绿 / WARN 橙 / CRIT 红 / INFO 灰蓝，别处不再发明新色。

注意顺序：QSS 中同优先级的选择器靠后的生效，
所以 `QWidget` 的透明底必须写在 `QWidget#Root`、`QMainWindow` 之前。
"""
from __future__ import annotations

import os
import tempfile

ACCENT = "#2f6fed"
ACCENT_DARK = "#2560d8"
ACCENT_SOFT = "#eef3ff"      # 品牌色的浅底（选中态 / 徽章）
TEXT = "#1f2329"
TEXT_MUTED = "#6b7280"
TEXT_FAINT = "#9aa0a6"
BG = "#f5f6f8"
CARD = "#ffffff"
BORDER = "#e6e8ec"
BORDER_STRONG = "#d7dae0"

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


def _branch_arrow_assets() -> tuple[str, str]:
    """生成树的展开/收起箭头 SVG，返回 (收起, 展开) 两个文件的路径。

    QSS 一旦写了 `::branch` 规则，Qt 就改用样式表绘制分支区，不指定
    image 时默认三角箭头会直接消失——所以必须自带两张小图。
    放临时目录，随启动重新生成，不污染安装目录。
    """
    try:
        d = os.path.join(tempfile.gettempdir(), "DiskSentinel_theme")
        os.makedirs(d, exist_ok=True)
        closed = os.path.join(d, "arrow_closed.svg").replace("\\", "/")
        opened = os.path.join(d, "arrow_open.svg").replace("\\", "/")
        tpl = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="9" height="9">'
            '<path d="{d}" fill="none" stroke="#9aa0a6" stroke-width="1.4" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )
        with open(closed, "w", encoding="utf-8") as f:
            f.write(tpl.format(d="M3 1.8 L7 4.5 L3 7.2"))   # 右向小箭头 ▸
        with open(opened, "w", encoding="utf-8") as f:
            f.write(tpl.format(d="M1.8 3 L4.5 7 L7.2 3"))   # 下向小箭头 ▾
        return closed, opened
    except OSError:
        return "", ""


_ARROW_CLOSED, _ARROW_OPEN = _branch_arrow_assets()
_BRANCH_RULES = (
    f'QTreeWidget::branch:closed:has-children {{ image: url("{_ARROW_CLOSED}"); }}\n'
    f'QTreeWidget::branch:open:has-children {{ image: url("{_ARROW_OPEN}"); }}'
    if _ARROW_CLOSED
    else ""
)


QSS = f"""
QWidget {{
    background: transparent;
    color: {TEXT};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}}
QLabel#AppTitle {{
    font-size: 16px;
    font-weight: 700;
    letter-spacing: 2px;
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
    font-size: 22px;
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
    font-weight: 600;
    padding-left: 4px;
}}

/* ---------- 按钮 ---------- */
QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 7px 16px;
    color: {TEXT};
}}
QPushButton:hover {{
    background: #f7f9fc;
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton:focus {{
    border-color: {ACCENT};
}}
/* 顶栏连体分段开关：左键右圆角、右键左圆角，中间不画分隔线 */
QPushButton#ModeFirst, QPushButton#ModeSecond {{
    padding: 7px 24px;
    font-weight: 500;
}}
QPushButton#ModeFirst {{
    border-radius: 8px 0 0 8px;
    border-right: none;
}}
QPushButton#ModeSecond {{
    border-radius: 0 8px 8px 0;
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
    background: {ACCENT_SOFT};
}}
/* 顶栏的「进行中」胶囊：浅蓝底 + 左侧小圆点，点击跳回运行中的页面 */
QPushButton#ActivityPill {{
    background: #e8f0ff;
    border: 1px solid #c9dcff;
    border-radius: 12px;
    color: {ACCENT_DARK};
    font-size: 12px;
    font-weight: 600;
    padding: 4px 14px 4px 12px;
}}
QPushButton#ActivityPill:hover {{
    background: #dbe8ff;
    border-color: {ACCENT};
    color: {ACCENT_DARK};
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
    background: transparent;
    color: {ACCENT_DARK};
    text-decoration: underline;
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

/* ---------- 列表 ---------- */
QListWidget {{
    background: {CARD};
    border: none;
    outline: none;
}}
QListWidget::item {{
    border-radius: 8px;
    padding: 3px;
    margin: 2px 4px;
}}
QListWidget::item:hover {{
    background: #f7f9fc;
}}
QListWidget::item:selected {{
    background: {ACCENT_SOFT};
}}

/* ---------- 页签 ---------- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 10px;
    background: {CARD};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    padding: 8px 18px;
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

/* ---------- 表格 ---------- */
QTableWidget {{
    background: {CARD};
    border: none;
    gridline-color: #f2f3f5;
    alternate-background-color: #fafbfc;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
QTableWidget::item {{
    padding: 5px 8px;
}}
QHeaderView::section {{
    background: #fafbfc;
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px 8px;
    color: {TEXT_MUTED};
    font-weight: 500;
}}

/* ---------- 树 ---------- */
QTreeWidget {{
    background: {CARD};
    border: none;
    outline: none;
    alternate-background-color: #fafbfc;
}}
QTreeWidget::item {{
    padding: 5px 4px;
}}
QTreeWidget::item:hover {{
    background: #f7f9fc;
}}
QTreeWidget::item:selected {{
    background: {ACCENT_SOFT};
    color: {TEXT};
}}
QTreeWidget::branch {{
    background: transparent;
}}
QTreeWidget::branch:selected {{
    background: {ACCENT_SOFT};
}}
{_BRANCH_RULES}

/* ---------- 输入控件 ---------- */
QPlainTextEdit, QTextBrowser {{
    background: {CARD};
    border: none;
    selection-background-color: {ACCENT_SOFT};
}}
QComboBox {{
    background: {CARD};
    border: 1px solid {BORDER_STRONG};
    border-radius: 7px;
    padding: 5px 12px;
    min-width: 140px;
}}
QComboBox:hover {{
    border-color: {ACCENT};
}}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 7px;
    selection-background-color: {ACCENT_SOFT};
    outline: none;
}}
QCheckBox {{
    spacing: 6px;
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

/* ---------- 状态栏 / 滚动条 / 提示 ---------- */
QStatusBar {{
    background: #fafbfc;
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QStatusBar::item {{
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #d7dae0;
    border-radius: 4px;
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
    height: 8px;
}}
QScrollBar::handle:horizontal {{
    background: #d7dae0;
    border-radius: 4px;
}}
QToolTip {{
    background: #2b3038;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
}}
QMainWindow, QDialog {{
    background: {BG};
}}
"""
