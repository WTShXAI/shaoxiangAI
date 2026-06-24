@echo off
chcp 65001 >nul
echo.
echo ═══════════════════════════════════════════════════════
echo   哨响AI v4.0 — 快速测试
echo ═══════════════════════════════════════════════════════

set PYTHON=C:\Users\ShXAI\.workbuddy\binaries\python\versions\3.13.12\python.exe
cd /d "%~dp0..\footballAI"

echo   执行全量测试 (498用例)...
%PYTHON% tests\test_v4_modules.py

echo.
echo   完成。
pause
