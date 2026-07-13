"""E1 P1-10 回归守卫 — feature_cols 必须与 match_features 表列一致.

防止 77 维特征契约漂移导致特征提取静默退化为纯规则(模型永不触发).
CI 跑 `pytest tests/` 即覆盖; DB 列与 feature_cols 不一致会 FAIL.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_feature_cols_subset_of_match_features():
    from pipeline.feature_consistency import verify_feature_cols
    ok, missing = verify_feature_cols(strict=False)
    assert ok is True, f"feature_cols 缺失于 match_features: {missing[:10]}"
    assert len(missing) == 0


def test_feature_cols_count_is_77():
    from pipeline.feature_consistency import load_feature_cols
    fc = load_feature_cols()
    assert fc is not None, "未加载到 draw_expert feature_cols"
    assert len(fc) == 77, f"feature_cols 应为 77 维, 实为 {len(fc)}"
