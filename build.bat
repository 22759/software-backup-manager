@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  一键打包 SoftwareManager.exe
REM  用法：双击本文件，或在命令行执行 build.bat
REM ============================================================

cd /d "%~dp0"

REM 优先用 Python314（已验证可用），否则退回 PATH 里的 python
set "PY=C:\Users\123\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" (
    for %%I in (python.exe) do set "PY=%%~$PATH:I"
)
if not exist "%PY%" (
    echo [错误] 找不到 Python，请先安装 Python 3.9+ 并加入 PATH
    pause
    exit /b 1
)

echo [1/4] 使用解释器: %PY%
"%PY%" -c "import sys; print('Python', sys.version)"

echo [2/4] 安装依赖...
"%PY%" -m pip install --upgrade pywebview pyinstaller >nul
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络
    pause
    exit /b 1
)

echo [3/4] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/4] 打包中（首次可能需要几分钟）...
"%PY%" -m PyInstaller --noconfirm --onefile --windowed ^
  --name SoftwareManager ^
  --icon icon.ico ^
  --add-data "web;web" ^
  app.py

if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

echo.
echo [完成] 产物: dist\SoftwareManager.exe
for %%A in (dist\SoftwareManager.exe) do echo       大小: %%~zA 字节
echo.
echo 提示：把 dist\SoftwareManager.exe 单独拷走也能运行，
echo       但 web\ 目录会被打进 exe 内部，无需额外拷贝。
pause
