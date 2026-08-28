"""
evaluate_model.py — 任意模型接入评估框架的单一入口 (SSoT)

evaluate_model(predict_fn, ...) 加载历史数据集, 对每场调 predict_fn 取 H/D/A 概率,
复用 metrics 模块算全部量化指标, 并与"市场去水基线"在**同一批比赛**上对比,
输出模型是否优于市场、以及 Value Bet 策略 ROI/Sharpe。

predict_fn 契约: fn(home, away, odds_h, odds_d, odds_a) -> (p_h, p_d, p_a)
  (概率无需先验归一化, 本函数内部会归一化; 但建议调用方已归一)
"""
import json
import os
from typing import Callable

from .metrics import (
    devig, log_loss, brier_score, accuracy, auc_ovr,
    calibration_curve, simulate_strategy,
)
from .backtest import load_dataset, _SPEC_START, _SPEC_END, DEFAULT_DB

REPORT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)


def _norm(p):
    s = sum(p)
    if s <= 0:
        return [1 / 3, 1 / 3, 1 / 3]
    return [x / s for x in p]


def evaluate_model(predict_fn: Callable,
                   model_name: str = "unknown_model",
                   db_path=DEFAULT_DB,
                   date_start=_SPEC_START, date_end=_SPEC_END,
                   value_thresh=0.02,
                   out_name=None):
    """对 predict_fn 在历史数据集上评估, 同集对比市场基线。返回报告 dict。"""
    data = load_dataset(db_path, date_start, date_end)
    n = len(data)

    model_probs, implied_probs, outcomes = [], [], []
    for d in data:
        imp = devig(d["oh"], d["od"], d["oa"])
        if imp is None:
            continue
        try:
            ph, pd, pa = predict_fn(d.get("home", ""), d.get("away", ""),
                                    d["oh"], d["od"], d["oa"])
        except Exception:
            continue
        mp = _norm([ph, pd, pa])
        model_probs.append(mp)
        implied_probs.append(imp)
        outcomes.append(d["result"])

    def _summarize(probs, impl, outs):
        return {
            "log_loss": round(log_loss(probs, outs), 4),
            "brier": round(brier_score(probs, outs), 4),
            "accuracy": round(accuracy(probs, outs), 4),
            "auc": {k: (round(v, 4) if v is not None else None)
                    for k, v in auc_ovr(probs, outs).items()},
            "calibration_H": calibration_curve(probs, outs),
        }

    model_sum = _summarize(model_probs, implied_probs, outcomes)
    base_sum = _summarize(implied_probs, implied_probs, outcomes)
    model_strat = simulate_strategy(model_probs, implied_probs, outcomes, value_thresh)
    base_strat = simulate_strategy(implied_probs, implied_probs, outcomes, value_thresh)

    report = {
        "meta": {
            "model_under_test": model_name,
            "date_window": f"{date_start}..{date_end}",
            "n_matches": n,
            "note": "与市场去水基线同集对比; 负数 delta = 模型优于市场",
        },
        "model": model_sum,
        "market_baseline": base_sum,
        "delta_vs_market": {
            "log_loss": round(model_sum["log_loss"] - base_sum["log_loss"], 4),
            "brier": round(model_sum["brier"] - base_sum["brier"], 4),
            "accuracy": round(model_sum["accuracy"] - base_sum["accuracy"], 4),
            "auc_macro": round(model_sum["auc"]["macro"] - base_sum["auc"]["macro"], 4),
        },
        "value_strategy_model": model_strat,
        "value_strategy_market": base_strat,
    }
    out_name = out_name or f"eval_{model_name.replace(' ', '_')}_report.json"
    out_path = os.path.join(REPORT_DIR, out_name)
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    report["_out_path"] = out_path
    return report
