#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G3 真实累积层幂等守护 — pytest 包装 (subprocess 调单一事实源, 被 pytest tests/ 收集)."""
import subprocess
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_live_accumulator_idempotent():
    script = os.path.join(_ROOT, "scripts", "test_live_accumulator.py")
    r = subprocess.run([sys.executable, script], cwd=_ROOT)
    assert r.returncode == 0, "live accumulator 幂等守护单测失败"
