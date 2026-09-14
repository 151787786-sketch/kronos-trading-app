@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title Kronos 股票交易助手

set "KRONOS_DIR=D:\a\kronos"
set "PYTHON=%KRONOS_DIR%\.venv\Scripts\python.exe"
set "PORT=7071"
set "URL=http://localhost:%PORT%"
set "LOGDIR=%KRONOS_DIR%\trading-app\logs"
set "LOG=%LOGDIR%\app.log"
set "KRONOS_NO_BROWSER=1"
set /a RESTART=0

if not exist "%PYTHON%" (
    echo [错误] 找不到 Python 环境: %PYTHON%
    echo         请确认 .venv 目录存在。
    pause
    exit /b 1
)

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

echo ====================================================
echo   Kronos 股票交易助手
echo   网址: %URL%
echo   日志: %LOG%
echo   停止: 关闭本窗口（或按 Ctrl+C）
echo ====================================================
echo.

netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [提示] 端口 %PORT% 已在运行，直接打开浏览器。
    start "" "%URL%"
    echo.
    echo 如果页面打不开，可能是别的程序占用了端口。查看占用者：
    echo     netstat -ano ^| findstr ":%PORT% "
    echo.
    pause
    exit /b 0
)

cd /d "%KRONOS_DIR%\trading-app"
echo 正在启动，浏览器会自动打开 %URL% ...
echo 关闭本窗口即停止服务。
echo ----------------------------------------------------
echo.

:: 等 4 秒再打开浏览器，避免服务还没起来
start "" cmd /c "timeout /t 4 /nobreak >nul & start "" "%URL%""

:loop
"%PYTHON%" app.py
set "EC=!errorlevel!"

echo.
if "!EC!"=="0" goto done

set /a RESTART+=1
if !RESTART! GEQ 3 (
    echo ----------------------------------------------------
    echo 服务连续失败 !RESTART! 次，已停止自动重启。
    echo 请把上面的错误信息，或日志文件里的内容发给开发者：
    echo   %LOG%
    pause
    exit /b !EC!
)

echo ----------------------------------------------------
echo 服务异常退出（错误码 !EC!），5 秒后自动重启（第 !RESTART! 次）...
echo 想中止请按 Ctrl+C 或直接关闭本窗口。
timeout /t 5 /nobreak >nul
goto loop

:done
echo ----------------------------------------------------
echo 服务已正常停止。
echo.
pause
