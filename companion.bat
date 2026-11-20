@echo off
chcp 65001 >nul
title 对白 · 启动全部服务
cd /d "%~dp0"

echo ========================================
echo   对白 · 启动全部服务
echo ========================================
echo.
echo [1/2] 启动后端服务 (端口 8002)...
rem Both `start` lines are pure ASCII, and neither mentions a .cmd or .bat file.
rem Two separate traps, both measured:
rem   1. chcp 65001 makes cmd read this file as UTF-8 while `start` parses its
rem      arguments as ANSI, so any Chinese character on the line corrupts the
rem      command and the window opens as a bare cmd.exe.
rem   2. `start` sees a .cmd in the command line and hands the whole thing to
rem      `cmd /c`, so PowerShell never starts and the window title becomes npm's.
rem Hence the ASCII titles, and `node vite.js` instead of `npx.cmd vite`.
start "DuiBai Backend" powershell -NoExit -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8002"

echo [2/2] 启动前端开发服务器 (端口 5176)...
if not exist "frontendV2\node_modules" goto no_frontend
start "DuiBai Frontend" powershell -NoExit -Command "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Set-Location -LiteralPath 'frontendV2'; node 'node_modules\vite\bin\vite.js' --port 5176"
goto started

:no_frontend
echo   [跳过] frontendV2\node_modules 不存在, 请先执行: cd frontendV2 ^&^& npm install

:started
echo.
echo ========================================
echo   后端: http://localhost:8002
if exist "frontendV2\node_modules" goto skip_front_url
goto skip_front_url_done
:skip_front_url
echo   前端: http://localhost:5176
:skip_front_url_done
echo.
echo   关闭此窗口不会影响已启动的服务。
echo   如需停止服务，请关闭对应窗口。
echo ========================================
echo.
