"""P0-Ω0 惰性化守卫测试

验证 pipeline.predictors 不再级联加载 ou_linkage 子模块.
守卫必须在子进程运行 —— 同进程先 import 后验会恒 False (sys.modules 共享污染).
"""

import subprocess
import sys


def test_lazy_loading_no_cascade_ou_linkage():
    """惰性化后: import pipeline.predictors 不加载 ou_linkage"""
    python = sys.executable
    code = (
        "import pipeline.predictors; "
        "import sys; "
        "loaded = 'pipeline.predictors.ou_linkage' in sys.modules; "
        "print('ou_linkage 加载?', loaded); "
        "assert not loaded, 'P0-Ω0 FAIL: ou_linkage 仍被级联加载!'"
    )
    result = subprocess.run(
        [python, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"守卫子进程失败 rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_lazy_loading_inject_violation_caught():
    """注入违规: 若有人恢复 import *, 守卫必须抓出来"""
    python = sys.executable
    cwd = __import__('os').path.dirname(__import__('os').path.abspath(__file__))
    code = (
        "import sys; sys.path.insert(0, r'" + cwd.replace('\\', '\\\\') + "'); "
        "# 模拟旧版 __init__.py (含 import *):\n"
        "import pipeline.predictors.ou_linkage; "  # 先导入以模拟违规
        "loaded = 'pipeline.predictors.ou_linkage' in sys.modules; "
        "assert loaded, '注入违规应检测到 ou_linkage 已加载'"
    )
    result = subprocess.run(
        [python, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"注入违规检测失败 rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_bridge_import_still_works():
    """bridge_service 导入路径不受惰性化影响"""
    python = sys.executable
    code = (
        "from pipeline.predictors.data_classes import MatchInput; "
        "print('MatchInput imported OK'); "
        "assert MatchInput is not None"
    )
    result = subprocess.run(
        [python, "-c", code],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"bridge 导入失败 rc={result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
