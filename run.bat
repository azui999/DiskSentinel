@echo off
setlocal
cd /d "%~dp0"

rem 优先使用项目自带的虚拟环境；不存在时自动初始化（需要本机有 Python 3.12+，联网一次）
set "PYEXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYEXE%" (
    echo [DiskSentinel] 没有找到虚拟环境 .venv，正在尝试创建，请稍候...
    py -3 -m venv .venv 2>nul
    if not exist "%PYEXE%" python -3 -m venv .venv 2>nul
    if not exist "%PYEXE%" (
        echo.
        echo 创建失败：本机没有可用的 Python 3.12 或更高版本。
        echo 请先安装 Python：https://www.python.org/downloads/
        echo 安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
        pause
        exit /b 1
    )
    echo [DiskSentinel] 正在安装依赖 PySide6 / pyqtgraph，首次需要几分钟...
    "%PYEXE%" -m pip install -r requirements.txt
)

rem 优先 pythonw（无控制台窗口）。异常都会写入 data\logs\error.log 并弹窗提示，
rem 所以不需要保留黑色控制台。
set "GUIEXE=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%GUIEXE%" set "GUIEXE=%PYEXE%"
start "" "%GUIEXE%" main.py
endlocal
