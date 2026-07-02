@echo off
chcp 65001 >nul
title 哨响AI v6.0 服务

set PROJECT_ROOT=D:\Architecture v4.0

:: 环境变量默认值
if not defined SECRET_KEY set SECRET_KEY=dev-placeholder-change-me
if not defined DEBUG set DEBUG=true
if not defined API_PORT set API_PORT=9000
set CUDA_VISIBLE_DEVICES=
set PYTHONPATH=%PROJECT_ROOT%\backend;%PROJECT_ROOT%

:: Python 选择
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
echo ============================================
echo   哨响AI v6.0 本地服务启动
echo   地址: http://localhost:%API_PORT%
echo   文档: /api/v1/docs
echo ============================================
echo.

"%PYTHON%" serve.py --host 0.0.0.0 --port %API_PORT%
pause
