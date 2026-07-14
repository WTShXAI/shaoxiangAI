"""
G10 pytest wrapper — 单一事实源: 调用 scripts/test_g10_softline_predict.py
被 pytest tests/ 自动收集; rc==0 即全部通过.
"""
import os
import subprocess
import sys


def test_g10_softline_predict():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "test_g10_softline_predict.py")
    rc = subprocess.run(
        [sys.executable, script],
        cwd=repo,
        capture_output=True,
        text=True,
    ).returncode
    assert rc == 0, f"G10 softline->predict 衔接测试失败 (rc={rc})"
