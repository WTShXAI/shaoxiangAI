@echo off
:: FootballAI 服务启动脚本 (独立持久化进程) - v5.0
set SECRET_KEY=FootballAI-v5.0-DGate-Production-2026-06-20-SecureKey
set PYTHONPATH=D:\Architecture v4.0;D:\AI\footballAI
cd /d "D:\Architecture v4.0"
d:\AI\footballAI\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --log-level info
