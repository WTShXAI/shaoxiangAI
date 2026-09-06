@echo off
REM ============================================
REM 哨响AI bridge_service 自动重启脚本
REM 使用: restart_bridge.bat
REM ============================================
setlocal enabledelayedexpansion

set PROJECT_ROOT=D:\Architecture

echo [哨响AI] Bridge 重启中...

REM 1. 查找 bridge_service 进程PID (venv parent + system child)
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq python.exe" /fo table /nh ^| findstr /i "bridge_service"') do (
    echo [哨响AI] 关闭 PID=%%a ...
    taskkill /f /pid %%a >nul 2>&1
)

REM 2. 等待端口释放
:wait_port
timeout /t 2 /nobreak >nul
netstat -ano | findstr ":9000.*LISTENING" >nul
if %errorlevel% equ 0 (
    echo [哨响AI] 端口 9000 仍占用, 等待...
    goto wait_port
)
echo [哨响AI] 端口 9000 已释放.

REM 3. 启动 bridge
echo [哨响AI] 启动 bridge_service ...
start "哨响AI-Bridge" /min cmd /c "cd /d %PROJECT_ROOT% && .venv\Scripts\python.exe bridge_service.py > %PROJECT_ROOT%\_uvicorn_9000.log 2>&1"

REM 4. 健康检查
echo [哨响AI] 等待服务就绪...
:wait_health
timeout /t 3 /nobreak >nul
curl -s -m 5 -o nul -w "%%{http_code}" http://localhost:9000/health | findstr "200" >nul
if %errorlevel% neq 0 (
    goto wait_health
)
echo [哨响AI] Bridge 重启完成!")
echo [哨响AI] Done.
