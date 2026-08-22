@echo off
chcp 65001 >nul
setlocal
title Kronos Web UI - 金融K线大模型

set "KRONOS_DIR=D:\a\kronos"
set "PYTHON=%KRONOS_DIR%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo [ERROR] Python venv not found: %PYTHON%
    echo Please run: python -m venv %KRONOS_DIR%\.venv
    pause
    exit /b 1
)

echo ====================================================
echo   启动 Kronos Web UI（金融 K 线基础大模型）
echo   地址: http://localhost:7070
echo   关闭: 在窗口按 Ctrl+C
echo ====================================================
echo.

cd /d "%KRONOS_DIR%\webui"

"%PYTHON%" launch.py

echo.
echo 服务已停止。
pause
