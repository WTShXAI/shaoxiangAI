"""单测 双源 ROI 对齐 + 脏行写入护栏 (事故⑦, T09 + T13).

断言:
  - 同 (match,market,timestamp) 对齐两源; |ΔROI| > 阈值(默认5.0pp) → DISPUTED
  - 偏差在阈值内 → UNIFIED
  - 输出必带 source + confidence
  - validate_roi_record 拒绝各类脏行
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.compute_value_layer import (  # noqa: E402
    align_dual_source_roi, validate_roi_record, Source,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError("FAILED: " + msg)
    print("  PASS:", msg)


def test_align_disputed() -> None:
    rows = [
        {"match_id": "m1", "market": "ah", "timestamp": "2026-01-01",
         "source": "LEYU", "roi": 20.0, "n": 100},
        {"match_id": "m1", "market": "ah", "timestamp": "2026-01-01",
         "source": "LEISU", "roi": 8.0, "n": 100},
    ]
    out = align_dual_source_roi(rows)
    _assert(len(out) == 1, "对齐为 1 条")
    rec = out[0]
    _assert(rec["disputed"] is True, "|ΔROI|=12 > 5 → DISPUTED")
    _assert(rec["source"] == Source.DISPUTED.value, "source=DISPUTED")
    _assert(rec["verdict"] == "DISPUTED", "verdict=DISPUTED")
    _assert(0.0 <= rec["confidence"] <= 1.0, "带 confidence(0..1)")
    _assert(abs(rec["delta_roi"] - 12.0) < 1e-6, "ΔROI=12.0")


def test_align_aligned() -> None:
    rows = [
        {"match_id": "m2", "market": "ah", "timestamp": "2026-01-02",
         "source": "LEYU", "roi": 10.0, "n": 100},
        {"match_id": "m2", "market": "ah", "timestamp": "2026-01-02",
         "source": "LEISU", "roi": 12.0, "n": 100},
    ]
    out = align_dual_source_roi(rows)
    rec = out[0]
    _assert(rec["disputed"] is False, "|ΔROI|=2 < 5 → 不标 DISPUTED")
    _assert(rec["source"] == Source.UNIFIED.value, "source=UNIFIED")
    _assert(rec["verdict"] == "ALIGNED", "verdict=ALIGNED")


def test_align_single_source() -> None:
    rows = [{"match_id": "m3", "market": "ou", "timestamp": "2026-01-03",
             "source": "LEYU", "roi": 15.0, "n": 50}]
    out = align_dual_source_roi(rows)
    rec = out[0]
    _assert(rec["source"] == "LEYU", "单源保留该源")
    _assert(rec["disputed"] is False, "单源不标 DISPUTED")


def test_validate_guard() -> None:
    clean = {"match_id": "x", "market": "ah", "timestamp": "t",
             "roi": 5.0, "source": "LEYU"}
    _assert(validate_roi_record(clean)[0] is True, "干净行通过护栏")
    _assert(validate_roi_record({"market": "ah", "timestamp": "t", "roi": 5.0,
                                 "source": "LEYU"})[0] is False, "缺 match_id 拒绝")
    _assert(validate_roi_record({"match_id": "x", "market": "ah", "timestamp": "t",
                                 "roi": "abc", "source": "LEYU"})[0] is False,
            "roi 非数值拒绝")
    _assert(validate_roi_record({"match_id": "x", "market": "ah", "timestamp": "t",
                                 "roi": 5.0, "source": "XXX"})[0] is False,
            "非法 source 拒绝")
    _assert(validate_roi_record({"match_id": "x", "market": "ah", "timestamp": "t",
                                 "roi": 5.0, "source": "LEYU", "odds": -1})[0] is False,
            "odds<=0 拒绝")


if __name__ == "__main__":
    test_align_disputed()
    test_align_aligned()
    test_align_single_source()
    test_validate_guard()
    print("ALL DUAL-SOURCE ROI TESTS PASSED")
