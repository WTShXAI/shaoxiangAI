"""G6 live 初盘 drift 补完 — 接入 pytest 套件.

以 subprocess 调用 scripts/test_g6_drift_open.py (单一事实源),
确保 G6 修复在 CI 中被守护:
  - 跨语言队名归一 (英文 live 队名 -> odds_features 中文初盘)
  - bridge_service 下标 bug 修复 (OddsInput 对象属性访问)
  - drift_available 显式标注
任何破坏上述逻辑的改动会在 push/PR 到 main 时让本测试变红.

自包含临时 DB, 不依赖 452MB football_data.db, CI 可移植.
"""
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "test_g6_drift_open.py")


def test_g6_drift_open():
    result = subprocess.run(
        [sys.executable, SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    assert result.returncode == 0, (
        f"G6 live 初盘 drift 回归失败 (rc={result.returncode}). 详见上方 PASS/FAIL 输出."
    )
