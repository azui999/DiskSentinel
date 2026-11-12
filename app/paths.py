"""统一路径解析：保证数据始终落在项目目录内，而不是当前工作目录。"""
from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    """项目根目录。

    以源码方式运行时是仓库根；若将来用 PyInstaller 打包，则取 exe 所在目录，
    避免把数据写进 Program Files 这类只读位置。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    d = project_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d
