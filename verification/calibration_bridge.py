"""桥接 eval_prediction_calibration 的校准指标 (T03).

零重写: 直接复用 SSoT 的 multi_metrics / diff_vs。
  rows_1x2 = [(p_home, p_draw, p_away, actual), ...]  → multi_metrics
  base/comp 指标字典                              → diff_vs
"""
from __future__ import annotations

from typing import List, Optional

from scripts.eval_prediction_calibration import multi_metrics, diff_vs


def calibration_for_rows(rows: List[tuple]) -> Optional[dict]:
    """rows = [(p_home, p_draw, p_away, actual)] → multi_metrics dict 或 None."""
    if not rows:
        return None
    return multi_metrics(rows)


def delta_vs(base: Optional[dict], comp: Optional[dict]) -> Optional[dict]:
    """comp 相对 base 的指标差 (负 LogLoss/Brier = 更好); 任一方缺失返回 None."""
    return diff_vs(base, comp)


__all__ = ["calibration_for_rows", "delta_vs"]
