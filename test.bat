@echo off
chcp 65001 >nul
echo.
echo ═══════════════════════════════════════════════════════
echo   哨响AI v6.0 — 快速测试
echo ═══════════════════════════════════════════════════════

REM 优先使用环境变量中的 Python, 否则从 PATH 查找
if defined PYTHON_EXE (set PYTHON=%PYTHON_EXE%) else (set PYTHON=python)

REM 仅检查当前项目测试文件
if exist "tests\test_v4_modules.py" (
    echo   执行本地测试...
    %PYTHON% tests\test_v4_modules.py
) else (
    echo   ❌ 未找到测试文件
    echo   请确保 tests\test_v4_modules.py 存在于当前目录
)

echo.
echo   完成。
pause