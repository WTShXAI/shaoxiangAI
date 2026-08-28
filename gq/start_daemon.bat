@echo off
REM GQ Collector Daemon — 单行启动（.venv）
cd /d D:\Architecture
REM 从 gitignored 的 gq/.env 读取 GQ_REQUEST_ID (令牌不入库)
for /f "usebackq tokens=*" %%i in ("gq\.env") do set "%%i"
.venv\Scripts\python.exe -u gq\auto_collector.py --daemon -i 60
