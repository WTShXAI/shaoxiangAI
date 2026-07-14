"""
G10 · soft-line 进 v7.1 predict() 衔接 — 自包含单测 (CI 可移植, 无重DB/无模型加载)

验证点:
  G10-1 护栏OFF(softline=None) -> predict() 完全等价现状(不覆盖)
  G10-2 分歧场(disagreement_detected=True) -> prediction 被 softline argmax 覆盖
  G10-3 一致场(disagreement_detected=False) -> 不覆盖(保持规则/市场 argmax)
  G10-4 分歧覆盖时附加 softline_applied / softline_adjusted_probs 元数据
  G10-5 softline 数据残缺(probs长度!=3) -> 不覆盖(保护现状)
  G10-6 不突变入参(返回新对象, 原 result.prediction 不变)
  G10-7 平局优先序与 parse_odds 一致 (p_h==p_d 时取 H)

模式: run_checks() 返回退出码, 供 pytest wrapper subprocess 调用 (单一事实源).
"""
import os
import sys
import dataclasses

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.engine import apply_softline_to_result, PredictionEngine  # noqa: E402


@dataclasses.dataclass
class _StubResult:
    prediction: str
    confidence: float = 0.5


class _StubEngine(PredictionEngine):
    """不加载真实 wc/league 模型, 仅验证 predict() 出口的 soft-line 衔接语义."""
    name = "stub"
    competition = "test"

    def __init__(self):
        self._loaded = True

    @property
    def loaded(self) -> bool:
        return True

    def predict(self, match, softline=None):
        result = _StubResult(prediction="H", confidence=0.7)
        return apply_softline_to_result(result, softline)


_FAILS = []


def _chk(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    if not cond:
        _FAILS.append(name)


def run_checks():
    print("=== G10 soft-line -> predict() 衔接单测 ===")
    eng = _StubEngine()

    # G10-1 护栏OFF: softline=None -> 不变
    r1 = eng.predict("match", softline=None)
    _chk("G10-1 护栏OFF(softline=None) prediction 不变=H", r1.prediction == "H")

    # G10-2 分歧场: D 概率最高 -> 覆盖为 D
    sl_d = {"disagreement_detected": True, "softline_adjusted_probs": [0.20, 0.55, 0.25]}
    r2 = eng.predict("match", softline=sl_d)
    _chk("G10-2 分歧场 softline 覆盖 -> D", r2.prediction == "D")

    # G10-3 一致场: disagreement=False -> 不覆盖
    sl_agree = {"disagreement_detected": False, "softline_adjusted_probs": [0.20, 0.55, 0.25]}
    r3 = eng.predict("match", softline=sl_agree)
    _chk("G10-3 一致场(disagreement=False) 不覆盖=H", r3.prediction == "H")

    # G10-4 分歧覆盖元数据
    _chk("G10-4 softline_applied=True", getattr(r2, "softline_applied", False) is True)
    _chk("G10-4 softline_adjusted_probs 回写", getattr(r2, "softline_adjusted_probs", None) == (0.20, 0.55, 0.25))

    # G10-5 数据残缺: probs 长度!=3 -> 不覆盖
    sl_bad = {"disagreement_detected": True, "softline_adjusted_probs": [0.5, 0.5]}
    r5 = eng.predict("match", softline=sl_bad)
    _chk("G10-5 probs 残缺 不覆盖=H", r5.prediction == "H")

    # G10-6 不突变入参
    base = _StubResult(prediction="A", confidence=0.6)
    apply_softline_to_result(base, sl_d)
    _chk("G10-6 不突变入参 (原对象 prediction 仍=A)", base.prediction == "A")

    # G10-7 平局优先序: p_h==p_d 且都 > p_a -> 取 H (与 parse_odds 一致)
    sl_tie = {"disagreement_detected": True, "softline_adjusted_probs": [0.40, 0.40, 0.20]}
    r7 = eng.predict("match", softline=sl_tie)
    _chk("G10-7 平局优先序 p_h==p_d 取 H", r7.prediction == "H")

    # G10-8 A 概率最高 -> 覆盖为 A
    sl_a = {"disagreement_detected": True, "softline_adjusted_probs": [0.25, 0.20, 0.55]}
    r8 = eng.predict("match", softline=sl_a)
    _chk("G10-8 分歧场 A 最高 -> 覆盖 A", r8.prediction == "A")

    if _FAILS:
        print(f"\n❌ G10 失败 {len(_FAILS)} 项: {_FAILS}")
        return 1
    print("\n✅ G10 全部通过 (8/8)")
    return 0


if __name__ == "__main__":
    sys.exit(run_checks())
