"""单测 LowWaterStateMachine (事故⑤, T07 + T13).

断言:
  - 无开盘价 → NEUTRAL, 绝不输出 TRAP (特尔纳瓦 2-2 打脸根因闭环)
  - 有开盘价 → 正常陷阱/价值逻辑
  - 输出必带 source + confidence
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.operator_signals import (  # noqa: E402
    LowWaterStateMachine, Verdict, LowWaterState, Source,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError("FAILED: " + msg)
    print("  PASS:", msg)


def test_no_opening() -> None:
    sm = LowWaterStateMachine(match_id="ternava")
    sm.feed(opening_price=None)
    _assert(sm.state is LowWaterState.NEED_OPENING, "无开盘价 → NEED_OPENING")
    out = sm.decide()
    _assert(out["verdict"] == Verdict.NEUTRAL.value, "无开盘价 → NEUTRAL")
    _assert(out["verdict"] != Verdict.TRAP.value, "无开盘价 绝不 TRAP (特尔纳瓦根因闭环)")
    _assert(isinstance(out["source"], str) and out["source"], "输出带 source 字段")
    _assert(isinstance(out["confidence"], (int, float)) and 0.0 <= out["confidence"] <= 1.0,
            "输出带 confidence(0..1)")
    _assert(out["confidence"] < 0.5, "无开盘价 confidence 低(依据不足)")


def test_low_water_trap() -> None:
    sm = LowWaterStateMachine(match_id="m2")
    sm.feed(opening_price=0.80, current_price=0.74)  # 低水继续走低 → TRAP
    out = sm.decide()
    _assert(out["verdict"] == Verdict.TRAP.value, "低水走低 → TRAP")
    _assert(out["source"] == Source.UNIFIED.value, "有开盘价 source=UNIFIED")
    _assert(out["confidence"] > 0.0, "有开盘价带 confidence")


def test_low_water_value() -> None:
    sm = LowWaterStateMachine(match_id="m3")
    sm.feed(opening_price=0.82, current_price=0.95)  # 低水走高 → VALUE
    out = sm.decide()
    _assert(out["verdict"] == Verdict.VALUE.value, "低水走高 → VALUE")


def test_neutral_when_flat() -> None:
    sm = LowWaterStateMachine(match_id="m4")
    sm.feed(opening_price=1.20, current_price=1.21)  # 非低水平稳 → NEUTRAL
    out = sm.decide()
    _assert(out["verdict"] == Verdict.NEUTRAL.value, "非低水平稳 → NEUTRAL")


if __name__ == "__main__":
    test_no_opening()
    test_low_water_trap()
    test_low_water_value()
    test_neutral_when_flat()
    print("ALL LOW-WATER STATE MACHINE TESTS PASSED")
