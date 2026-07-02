@echo off
chcp 65001 >nul
title 哨响AI v4.1 服务

:: ── 项目路径 ──
set PROJECT_ROOT=D:\Architecture v4.0

:: ── 从 .env 加载环境变量（若不存在则用默认值） ──
if exist "%PROJECT_ROOT%\.env" (
    for /f "tokens=1,2 delims==" %%a in (%PROJECT_ROOT%\.env) do (
        if "%%a"=="SECRET_KEY" set %%a=%%b
        if "%%a"=="DEBUG" set %%a=%%b
        if "%%a"=="API_PORT" set %%a=%%b
    )
)
if not defined SECRET_KEY set SECRET_KEY=dev-placeholder-change-me
if not defined DEBUG set DEBUG=true
if not defined API_PORT set API_PORT=9000
set CUDA_VISIBLE_DEVICES=

set PYTHONPATH=%PROJECT_ROOT%;%PROJECT_ROOT%\backend

:: ── Python选择（仅使用项目自带虚拟环境） ──
if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe
) else (
    echo [错误] 未找到Python虚拟环境！
    echo 请在该目录下运行: python -m venv .venv
    pause
    exit /b 1
)

cd /d "%PROJECT_ROOT%"

echo.
echo ╔══════════════════════════════════════════╗
echo ║     哨响AI v4.1 本地部署启动              ║
echo ║     Python: %PYTHON%                      ║
echo