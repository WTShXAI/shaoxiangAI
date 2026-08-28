@echo off
REM ============================================================================
REM 哨响AI 守护卸载 (Windows Task Scheduler)
REM 删除定时任务, 并精准停止守护进程及其托管的 bridge_service / auto_collector.
REM 注意: 请用「管理员身份」运行本脚本.
REM ============================================================================
set "ROOT=%~dp0..\.."
set "TASK=ShaoXiangAIGuard"

echo [1/2] Deleting scheduled task "%TASK%"...
schtasks /delete /tn "%TASK%" /f 2>nul
if errorlevel 1 (
    echo [WARN] Task not found or could not delete (maybe already removed).
) else (
    echo [OK] Task deleted.
)

echo [2/2] Stopping guard + managed processes (by command-line keyword)...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'daemon_guard|bridge_service|auto_collector' } | ForEach-Object { taskkill /PID $_.ProcessId /F /T 2>&1 }"
echo.
echo [OK] Guard uninstalled and processes stopped.
pause
