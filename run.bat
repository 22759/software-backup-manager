@echo off
chcp 65001 >nul
setlocal

set "EXE=%~dp0SoftwareManager.exe"
set "PY=%~dp0..\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=C:\Users\123\AppData\Local\Programs\Python\Python314\python.exe"
set "PYW=%PY:python.exe=pythonw.exe%"
set "APP=%~dp0app.py"

REM 优先启动打包好的 exe（--windowed，本身无控制台，不会有黑框）
if exist "%EXE%" (
    REM 非管理员时自动提权（UAC 弹窗），已管理员则直接运行
    net session >nul 2>&1
    if errorlevel 1 (
        powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -FilePath '%EXE%' -Verb RunAs -WindowStyle Hidden" >nul 2>&1
    ) else (
        start "" "%EXE%"
    )
) else (
    REM 若缺少 pywebview 则自动安装（用 python.exe，因为要看到 pip 输出）
    "%PY%" -c "import webview" >nul 2>&1
    if errorlevel 1 (
        echo 正在安装 pywebview 依赖...
        "%PY%" -m pip install pywebview
    )
    REM 用 pythonw.exe 启动，避免残留一个黑色命令行窗口
    start "" "%PYW%" "%APP%"
)
exit /b
