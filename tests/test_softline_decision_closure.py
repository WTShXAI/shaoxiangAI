"""P0-1 soft-line 决策闭环回归 — 接入 pytest 套件.

以 subprocess 调用 scripts/test_softline_decision_closure.py (单一事实源),
确保 P0-1 修复 (soft-line 淡化概率回灌主 1X2 决策) 在 CI 中被守护:
任何破坏 soft-line 闭环的改动都会在 push/PR 到 main 时让本测试变红.

被现有 CI 的 `pytest tests/` 自动收集 (ci.yml / test.yml 均覆盖).
"""
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "test_softline_decision_closure.py")


def test_p0_1_softline_decision_closure():
    result = subprocess.run(
        [sys.executable, SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # 透传脚本自身的结构化 PASS/FAIL 输出, 失败时可快速定位
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, (
        f"P0-1 soft-line 闭环回归失败 (rc={result.returncode}). 详见上方 PASS/FAIL 输出."
    )
