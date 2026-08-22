@echo off
chcp 936 >nul
setlocal
title Kronos Trading App - Push to GitHub

set "KRONOS_DIR=D:\a\kronos"
set "REPO_URL=%1"

if "%REPO_URL%"=="" (
    echo.
    echo 使用方法：把 GitHub 仓库地址作为参数传入
    echo   例： 运行推送.bat https://github.com/你的用户名/kronos-trading-app.git
    echo.
    echo 或者直接编辑本文件，把下面的地址填进去：
    set "REPO_URL=https://github.com/你的用户名/kronos-trading-app.git"
)

cd /d "%KRONOS_DIR%"

echo ====================================================
echo   Kronos Trading App - Push to GitHub
echo   仓库: %REPO_URL%
echo ====================================================
echo.

REM 检查是否已有远程
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    git remote add origin "%REPO_URL%"
) else (
    git remote set-url origin "%REPO_URL%"
)

echo [1/3] 提交本地更改...
git add -A
git commit -m "update" 2>nul
echo [2/3] 推送到 GitHub...
git push -u origin master 2>&1
if errorlevel 1 (
    echo.
    echo [失败] 推送未成功。
    echo 常见原因：
    echo   1. 仓库地址错误
    echo   2. GitHub 需要登录 - 推送时会弹窗或要求输入用户名/token
    echo   3. 网络无法访问 github.com（可能需要代理）
    echo.
    pause
    exit /b 1
)
echo.
echo [3/3] 推送成功！🎉
echo 仓库地址: %REPO_URL%
pause
