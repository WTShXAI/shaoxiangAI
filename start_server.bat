@echo off
:: FootballAI 服务启动脚本 (独立持久化进程) - v5.2.14
:: SECRET_KEY/OCR 凭据等敏感信息统一由 .env 管理
set PYTHONPATH=D:\Architecture v4.0;D:\AI\footballAI
cd /d "D:\Architecture v4.0"
D:\Architecture v4.0\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level info
