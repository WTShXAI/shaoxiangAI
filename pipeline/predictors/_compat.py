"""numpy 统一导入模块 — Phase 0: 消除 _FakeArray 冗余

numpy 已是 requirements.txt 硬依赖，所有生产路径走 _HAS_NUMPY=True。
_FakeArray 从未被实际使用过——9个文件共享完全相同的 fallback 类，共108行死代码。
"""
import numpy as np  # noqa: F401
