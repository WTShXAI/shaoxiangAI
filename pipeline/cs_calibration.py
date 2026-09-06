#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pipeline/cs_calibration.py — 波胆概率校准
===========================================
用 3728 场真实赛果的波胆频率倒推庄家隐含概率的系统性偏离,
产出校准因子 (data/cs_calibration.json), 在 analysis_center 的
波胆推荐中后处理: p_cal = p_market * factor, 然后重归一化.

核心发现 (2026-08-04):
  - 0-0 被系统性低估 40% (因子=1.40): 实际14.6% vs 庄家10.4%
  - 所有其他比分(1-0~2-3)被系统性高估 (因子 0.22~0.76)
  - 比分越"好看"(2-1/3-0) 被高估越多

用法:
  from pipeline.cs_calibration import apply_calibration
  calibrated = apply_calibration(market_top3)  # market_top3=[{'score':'1-0','prob':13.8},...]
"""
import json, os

_CAL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cs_calibration.json")
_CACHE = None  # 模块级缓存, 加载一次


def load_calibration():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    if os.path.exists(_CAL_PATH):
        with open(_CAL_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        _CACHE = data.get("calibrated_scores", {})
    else:
        _CACHE = {}
    return _CACHE


def calibrate_score(score: str, prob: float) -> float:
    """对单个比分做校准: p_cal = p * factor (仅对可校准的比分)."""
    cal = load_calibration()
    if score in cal and cal[score]["n"] >= 30:
        factor = cal[score]["factor"]
        return prob * factor
    return prob  # 不可校准, 原值返回


def apply_calibration(market_top3: list) -> list:
    """对 market_top3 的每个比分概率做因子修正.
    不重归一化: 原始概率来自全量CS去水(本身就是完整分布), 仅对单分比分做偏差修正.
    返回列表保持不变序.
    """
    if not market_top3:
        return market_top3
    cal = load_calibration()
    calibrated = []
    for item in market_top3:
        score = item.get("score", "")
        prob = item.get("prob", 0.0)
        if score in cal and cal[score]["n"] >= 30:
            factor = cal[score]["factor"]
            # 温和修正: 取因子与1.0的中点, 避免单个比分校准过度放大/缩小
            adj_factor = 1.0 + (factor - 1.0) * 0.5
            cal_prob = prob * adj_factor
            calibrated.append({**item, "prob": round(cal_prob, 1), "cal_factor": round(adj_factor, 2)})
        else:
            calibrated.append(item)
    return sorted(calibrated, key=lambda x: -x["prob"])


if __name__ == "__main__":
    # 自测: 对 example top3 做校准
    top3 = [
        {"score": "1-0", "prob": 13.8, "source": "market"},
        {"score": "2-0", "prob": 13.2, "source": "market"},
        {"score": "1-1", "prob": 10.0, "source": "market"},
    ]
    cal = load_calibration()
    print(f"已加载 {len(cal)} 个校准比分")
    print("校准前:", [(c['score'], c['prob']) for c in top3])
    result = apply_calibration(top3)
    print("校准后:", [(c['score'], c['prob'], c.get('cal_factor', '-')) for c in result])
