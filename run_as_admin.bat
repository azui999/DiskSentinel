@echo off
setlocal
cd /d "%~dp0"

rem 以管理员身份启动：smartctl 需要管理员权限才能读取错误日志、自检日志与容量等识别信息。
rem 属性数据本身在普通权限下也能读，所以 run.bat 也可用，只是数据少一部分。
set "PYEXE=%~dp0.venv\Scripts\python.exe"

if not exist "%PYEXE%" (
    echo [DiskSentinel] 请先双击 run.bat 运行一次，完成环境初始化后再使用本脚本。
    pause
    exit /b 1
)

rem 提权运行 pythonw（无控制台窗口），异常会写入 data\logs\error.log 并弹窗提示
set "GUIEXE=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%GUIEXE%" set "GUIEXE=%PYEXE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath '%GUIEXE%' -ArgumentList 'main.py' -Verb RunAs -WorkingDirectory '%~dp0'"
endlocal
