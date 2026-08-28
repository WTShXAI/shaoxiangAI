@echo off
REM ============================================================================
REM 哨响AI 守护安装 (Windows Task Scheduler)
REM 以 SYSTEM 身份在系统启动时自动拉起 deploy/windows/daemon_guard.py,
REM 由其常驻守护 bridge_service + gq/auto_collector (进程崩溃/冻结自动重启).
REM 注意: 请用「管理员身份」运行本脚本.
REM ============================================================================
set "ROOT=%~dp0..\.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
set "GUARD=%ROOT%\deploy\windows\daemon_guard.py"
set "TASK=ShaoXiangAIGuard"

if not exist "%PY%" (
    echo [ERROR] .venv python not found: %PY%
    echo         Please create venv first:  python -m venv .venv  &&  .venv\Scripts\pip install -r deploy\requirements.prod.txt
    pause
    exit /b 1
)

echo [1/2] Registering scheduled task "%TASK%" (on system start, run whether user logged on)...
schtasks /create /tn "%TASK%" /tr "\"%PY%\" \"%GUARD%\"" /sc onstart /ru SYSTEM /rl highest /f
if errorlevel 1 (
    echo [ERROR] Failed to create task. Run this script as Administrator.
    pause
    exit /b 1
)

echo [2/2] Starting task now...
schtasks /run /tn "%TASK%"
echo.
echo [OK] Guard installed and started. It will auto-run after every reboot.
echo      View:  taskschd.msc  ->  Task Scheduler Library  ->  %TASK%
echo      Logs:  %ROOT%\logs\daemon_guard.log
echo      Stop :  deploy\windows\uninstall_guard.bat
pause
