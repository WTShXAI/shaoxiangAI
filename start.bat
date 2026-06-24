@echo off
chcp 65001 >nul
title 哨响AI v4.1 服务

:: ── 环境变量 ──
set SECRET_KEY=FootballAI-v4.1-LocalDev-Deploy-2026-SecureKey-32chars+
set DEBUG=true
set API_PORT=8000
set CUDA_VISIBLE_DEVICES=

:: ── 路径 ──
set PROJECT_ROOT=D:\Architecture v4.0
set PYTHONPATH=%PROJECT_ROOT%;%PROJECT_ROOT%\backend

:: ── Python选择 (FootballAI venv 有完整依赖) ──
if exist "D:\AI\footballAI\.venv\Scripts\python.exe" (
    set PYTHON=D:\AI\footballAI\.venv\Scripts\python.exe
) else if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe
) else (
    echo [错误] 未找到Python虚拟环境！
    pause
    exit /b 1
)

cd /d "%PROJECT_ROOT%"

echo.
echo ╔══════════════════════════════════════════╗
echo ║     哨响AI v4.1 本地部署启动              ║
echo ║     Python: %PYTHON%                      ║
echo ║     端口:   %API_PORT%                     ║
echo ╚══════════════════════════════════════════╝
echo.

"%PYTHON%" main.py backend --dev --port %API_PORT%

pause
