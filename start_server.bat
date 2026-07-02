@echo off
:: 哨响AI 服务启动脚本 (独立进程) - v6.0
set PYTHONPATH=D:\Architecture\backend;D:\Architecture
cd /d "D:\Architecture"
"D:\Architecture\.venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 9000 --log-level info
