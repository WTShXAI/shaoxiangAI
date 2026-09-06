"""P0-3 分歧闸门 + 规范凯利 · 轻量 CI 回归断言 — 接入 pytest 套件.

以 subprocess 调用 scripts/test_p0_3_divergence_gate.py (单一事实源),
确保生产价值层(value_layer)的选边逻辑 + 分歧闸门 + 规范凯利 在 CI 中被守护:
  - 分歧闸门正确性 (disagreement_detected)
  - 规范凯利 / 防全押 (P0-3 根因bug)
  - 分歧闸门是 edge 过滤器 (P0-3 核心结论)
  - argmax@best 一致性自检
"""
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "test_p0_3_divergence_gate.py")


def test_p0_3_divergence_gate():
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
        f"P0-3 分歧闸门/规范凯利 回归失败 (rc={result.returncode}). 详见上方输出。"
    )
