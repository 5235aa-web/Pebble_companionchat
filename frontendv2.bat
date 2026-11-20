@echo off
chcp 65001 >nul
title 对白 · 前端 V2
cd /d "%~dp0"

echo ========================================
echo   对白 · 前端 V2 (端口 5176)
echo ========================================
echo.

if not exist "frontendV2\node_modules" (
    echo   [缺少依赖] frontendV2\node_modules 不存在
    echo.
    echo   请先安装依赖:
    echo     cd /d E:\companionchat\frontendV2
    echo     npm install
    echo.
    pause
    exit /b 1
)

echo   启动 Vite 开发服务器...
echo.
cd /d "%~dp0frontendV2"
call npx vite --port 5176

echo.
echo   前端已停止。
pause
