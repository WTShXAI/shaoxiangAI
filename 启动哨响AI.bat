@echo off
chcp 65001 >nul
title 哨响AI 足球预测系统

:: ════════════════════════════════════════════════════════
::  哨响AI 一键启动脚本
::  双击运行即可, 关闭窗口 = 停止服务
:: ════════════════════════════════════════════════════════

:: ── 项目路径 ──
set "PROJECT_ROOT=D:\Architecture v4.0"

:: ── Python 虚拟环境 ──
if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
) else (
    echo [错误] 未找到 Python 虚拟环境!
    echo 请确认 D:\Architecture v4.0\.venv\Scripts\python.exe 存在。
    echo 若不存在，请在该目录下运行: python -m venv .venv
    pause
    exit /b 1
)

:: ── 环境变量 ──
set "DEBUG=true"
set "API_HOST=0.0.0.0"
set "API_PORT=9000"
set "CUDA_VISIBLE_DEVICES="
set "JEPA_BLEND_WEIGHT=0.08"
set "THRESHOLD_BIAS_H=0.02"
set "THRESHOLD_BIAS_D=0.10"
set "THRESHOLD_BIAS_A=-0.04"

cd /d "%PROJECT_ROOT%"

echo.
echo ╔═══════════════════════════════════════════════════════╗
echo ║          哨响AI 足球预测系统 正在启动...              ║
echo ║                                                       ║
echo ║  前端页面:  http://localhost:3000                      ║
echo ║  后端API:   http://localhost:9000/api/v1/docs          ║
echo ║  Prometheus: http://localhost:9091                    ║
echo ║                                                       ║
echo ║  前端自动代理 /api → localhost:9000                    ║
echo ║  前端自动代理 /ws  → localhost:9000                    ║
echo ╚═══════════════════════════════════════════════════════╝
echo.

:: ── 启动后端服务 (端口 9000) ──
start "哨响AI-后端" /D "%PROJECT_ROOT%" "%PYTHON%" serve.py --port 9000

:: ── 启动前端开发服务器 (端口 3000, /D 直接设工作目录) ──
where npm >nul 2>nul
if %errorlevel% equ 0 (
    start "哨响AI-前端" /D "%PROJECT_ROOT%\frontend" cmd /c "npm run dev"
) else (
    echo [警告] 未找到 npm, 请确保 Node.js 已安装并加入 PATH
)

:: ── 等几秒后自动打开浏览器 ──
timeout /t 8 /nobreak >nul
start http://localhost:3000

:: ── 保持窗口不关闭 ──
echo.
echo 后端与前端的服务窗口已独立打开。
echo 关闭此窗口不会停止服务,请手动关闭后端/前端子窗口。
echo.
echo 按任意键关闭信息窗口...
pause >nul