@echo off
chcp 65001 >nul
echo.
echo ═══════════════════════════════════════════════════════
echo   哨响AI v5.2.14 — 快速测试
echo ═══════════════════════════════════════════════════════

REM 优先使用环境变量中的 Python, 否则从 PATH 查找
if defined PYTHON_EXE (set PYTHON=%PYTHON_EXE%) else (set PYTHON=python)

REM 测试目录优先当前项目, 否则 fallback 到 footballAI
if exist "tests\test_v4_modules.py" (
    echo   执行本地测试...
    %PYTHON% tests\test_v4_modules.py
) else if exist "%~dp0..\footballAI\tests\test_v4_modules.py" (
    cd /d "%~dp0..\footballAI"
    echo   执行 footballAI 测试...
    %PYTHON% tests\test_v4_modules.py
) else (
    echo   ❌ 未找到测试文件
    echo   请设置 FOOTBALLAI_ROOT 环境变量指向 footballAI 项目根目录
)

echo.
echo   完成。
pause
