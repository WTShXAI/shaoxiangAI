@echo off
:: 哨响AI 服务启动脚本 (独立进程) - v6.0
set PYTHONPATH=D:\Architecture v4.0;D:\Architecture v4.0\backend
cd /d "D:\Architecture v4.0"
"D:\Architecture v4.0\.venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 9000 --log-level info
