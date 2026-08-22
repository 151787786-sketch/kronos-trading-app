@echo off
chcp 936 >nul
setlocal
title Kronos Trading App - Test Suite

set "KRONOS_DIR=D:\a\kronos"
set "PYTHON=%KRONOS_DIR%\.venv\Scripts\python.exe"
set "APP_DIR=%KRONOS_DIR%\trading-app"

echo ====================================================
echo   Kronos Trading App - Automated Test Suite
echo   Running all test suites against http://localhost:7071
echo ====================================================
echo.

if not exist "%PYTHON%" (
    echo [ERROR] Python venv not found
    pause
    exit /b 1
)

netstat -ano | findstr ":7071 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Server is not running on port 7071.
    echo Please start it first: double-click 启动交易APP.bat
    pause
    exit /b 1
)

cd /d "%APP_DIR%"

set "PYTHONIOENCODING=utf-8"

echo.
echo [1/6] Regression (core APIs)...
"%PYTHON%" tests\regression.py
if errorlevel 1 set "ANYFAIL=1"

echo.
echo [2/6] Edge cases...
"%PYTHON%" tests\edge_cases.py
if errorlevel 1 set "ANYFAIL=1"

echo.
echo [3/6] Data correctness...
"%PYTHON%" tests\data_correctness.py
if errorlevel 1 set "ANYFAIL=1"

echo.
echo [4/6] Performance...
"%PYTHON%" tests\performance.py
if errorlevel 1 set "ANYFAIL=1"

echo.
echo [5/6] Security...
"%PYTHON%" tests\security.py
if errorlevel 1 set "ANYFAIL=1"

echo.
echo [6/6] Stability (takes ~4 minutes)...
"%PYTHON%" tests\stability.py
if errorlevel 1 set "ANYFAIL=1"

echo.
echo ====================================================
if defined ANYFAIL (
    echo RESULT: SOME SUITES FAILED - see output above
) else (
    echo RESULT: ALL SUITES PASSED
)
echo ====================================================
pause
