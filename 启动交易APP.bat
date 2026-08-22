@echo off
chcp 936 >nul
setlocal
title Kronos Trading App

set "KRONOS_DIR=D:\a\kronos"
set "PYTHON=%KRONOS_DIR%\.venv\Scripts\python.exe"
set "PORT=7071"
set "URL=http://localhost:%PORT%"

if not exist "%PYTHON%" (
    echo [ERROR] Python venv not found
    pause
    exit /b 1
)

echo ====================================================
echo   Kronos Stock Trading Assistant
echo   URL: %URL%
echo   Close: Ctrl+C
echo ====================================================
echo.

netstat -ano | findstr ":%PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [INFO] Port %PORT% already in use.
    start "" "%URL%"
    pause
    exit /b 0
)

cd /d "%KRONOS_DIR%\trading-app"
echo Starting, browser will open %URL% ...
echo.
"%PYTHON%" app.py
echo.
echo Server stopped.
pause