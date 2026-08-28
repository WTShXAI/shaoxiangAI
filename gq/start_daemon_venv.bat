@echo off
REM GQ Collector Daemon — 启动 .venv
cd /d D:\Architecture
call .venv\Scripts\activate.bat
echo.
echo === GQ 赔率采集器守护模式 ===
echo Python: .venv (含 playwright)
echo 间隔: 60秒
echo.
echo 停止: Ctrl+C 或 taskkill /F /IM python.exe
echo 日志: gq\daemon_v3.log
echo.
python -u gq\auto_collector.py --daemon -i 60
pause
