"""E1 P2-17 回归守卫 — soft-line (G10) 决策闭环契约 (SSoT: apply_softline_to_result).

锁定行为, 防止护栏逻辑被后续改动破坏:
  1. disagreement_detected=False 或 无 softline → result 完全不变.
  2. disagreement_detected=True + 合法 3 维 probs → 按 soft-line argmax 覆盖 prediction.
  3. probs 非法(非3维/空) → 不覆盖, 保持原 result.
"""
import dataclasses
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclasses.dataclass
class _FakeResult:
    prediction: str = "H"
    softline_applied: bool = False


def test_no_softline_unchanged():
    from pipeline.engine import apply_softline_to_result
    r = _FakeResult(prediction="H")
    out = apply_softline_to_result(r, None)
    assert out.prediction == "H"
    assert out.softline_applied is False


def test_disagreement_false_unchanged():
    from pipeline.engine import apply_softline_to_result
    r = _FakeResult(prediction="H")
    out = apply_softline_to_result(r, {"disagreement_detected": False, "softline_adjusted_probs": [0.1, 0.8, 0.1]})
    assert out.prediction == "H"


def test_disagreement_true_overrides_by_argmax():
    from pipeline.engine import apply_softline_to_result
    r = _FakeResult(prediction="H")
    # 平局概率最高 → 应覆盖为 D
    out = apply_softline_to_result(r, {"disagreement_detected": True, "softline_adjusted_probs": [0.1, 0.8, 0.1]})
    assert out.prediction == "D"
    assert out.softline_applied is True
    # 客胜最高
    out2 = apply_softline_to_result(r, {"disagreement_detected": True, "softline_adjusted_probs": [0.1, 0.2, 0.7]})
    assert out2.prediction == "A"


def test_malformed_probs_not_overridden():
    from pipeline.engine import apply_softline_to_result
    r = _FakeResult(prediction="H")
    out = apply_softline_to_result(r, {"disagreement_detected": True, "softline_adjusted_probs": [0.5, 0.5]})
    assert out.prediction == "H"
    out2 = apply_softline_to_result(r, {"disagreement_detected": True, "softline_adjusted_probs": []})
    assert out2.prediction == "H"
