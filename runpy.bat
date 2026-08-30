@echo off
REM 项目 Python 包装器 —— 强制使用 D:\Architecture\.venv 解释器。
REM 用法:  runpy script.py [args...]
REM 目的:  PATH 里裸 `python` 指向 WorkBuddy 自带 3.13(sklearn 1.9.0),
REM        与项目 venv(3.12.10 / sklearn 1.6.1) 依赖版本不同, 会静默给出错误结果。
REM        本脚本保证任何调用都落在正确的环境上。
"%~dp0.venv\Scripts\python.exe" %*
