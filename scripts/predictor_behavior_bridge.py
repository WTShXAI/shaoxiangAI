#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""predictor_behavior_bridge.py — 操盘手锚定 × 定价行为规则 融合桥 (零侵入)

作用:
  不改 unified_predictor 核心, 只在其 predict() 输出之后叠加一层"定价行为校准"。
  当调用方拥有赔率 tick 序列 (直播 live_poll / 电子盘 jsonl 同构) 时, 用
  extract_behavior() 提炼庄家时序定价行为指纹, 喂给 calibrate() 修正锚定概率。

为什么是锚定之后、不是塞进模型:
  行为特征是"庄家定价函数的行为指纹", 用于校准置信度, 而非直接预测赛果
  (避免 score 泄露 / 分布偏移)。这符合 unified_predictor "盘口最强单信号" 铁律。

编排层调用方式 (engine / ranked_predictor 的直播路径):
    from predictor_behavior_bridge import predict_with_behavior
    out = predict_with_behavior(
        ticks=tick_rows,                 # list[dict] (live_poll / electronic 同构)
        home=..., away=...,
        odds_h=..., odds_d=..., odds_a=...,   # 与 ticks 末态一致的当前/终盘赔率
        **kwargs,                        # 透传给 UnifiedPredictor.predict (odds2_*, open_*, ...)
    )
    # out["probabilities"]             : 原锚定最终概率 (温标后)
    # out["anchor_probabilities"]       : 操盘手去水锚定 (devig)
    # out["behavior_calibrated_probabilities"] : 叠加行为校准后的概率 (同口径温标)
    # out["behavior_delta"]             : 行为校准相对锚定的偏移 (pp)
    # out["behavior"]                   : 行为特征字典
    # out["behavior_used"]              : 是否有有效 tick 序列驱动校准

无 tick 序列时: 退化为纯锚定 (behavior_used=False, 校准层零副作用)。
"""
import os
import sys
import json
import glob

# 路径: 本文件在 scripts/, 项目根 = 父目录的父目录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SCR = os.path.join(ROOT, "scripts")
if SCR not in sys.path:
    sys.path.insert(0, SCR)

import numpy as np
from pipeline.predictors.unified_predictor import UnifiedPredictor, _temp_scale, CALIB_T
from pricing_behavior_features import extract_behavior
from behavior_calibrator import calibrate


def _last_1x2(rows):
    """取末个有 1X2 的 tick 的 1X2 赔率三元组 (h,d,a)。"""
    last = None
    for r in rows:
        mk = r.get("markets") or {}
        m = mk.get("1X2")
        if not isinstance(m, dict):
            continue
        for ks in (("h", "d", "a"), ("home", "draw", "away")):
            if all(k in m for k in ks):
                last = (m[ks[0]], m[ks[1]], m[ks[2]])
                break
        if last:
            break
    return last


def predict_with_behavior(ticks, home, away, odds_h, odds_d, odds_a, **kwargs):
    """融合入口: 操盘手锚定 + 定价行为校准。
    ticks: list[dict] 赔率 tick 序列 (可空)。其余参数透传给 UnifiedPredictor.predict。"""
    base = UnifiedPredictor().predict(home, away, odds_h, odds_d, odds_a, **kwargs)
    anchor = base.get("anchor_probabilities") or {"H": 0.0, "D": 0.0, "A": 0.0}
    anchor_arr = np.array([anchor.get("H", 0.0), anchor.get("D", 0.0), anchor.get("A", 0.0)], dtype=float)

    behavior = extract_behavior(ticks) if ticks else None
    if not behavior or not behavior.get("valid"):
        return {
            **base,
            "behavior": behavior,
            "behavior_calibrated_probabilities": {"H": anchor["H"], "D": anchor["D"], "A": anchor["A"]},
            "behavior_delta": {"H": 0.0, "D": 0.0, "A": 0.0},
            "behavior_used": False,
        }

    cal = calibrate(anchor, behavior)
    # 与 predictor 输出口径统一: 锚定是 devig 空间, probabilities 是温标后;
    # 行为校准作用在 devig 锚定上, 这里同样温标, 使 calibrated 与 probabilities 同口径可比。
    cal_scaled = _temp_scale(np.array([cal["H"], cal["D"], cal["A"]], dtype=float), T=CALIB_T)
    cal_prob = {"H": float(cal_scaled[0]), "D": float(cal_scaled[1]), "A": float(cal_scaled[2])}
    # 行为偏移隔离: 相对纯锚定预测 (base["probabilities"], 同为温标后口径),
    # 而非相对 devig 锚定 — 避免温标差距混入、伪装成行为贡献。
    base_prob = base.get("probabilities") or anchor
    delta = {
        "H": round(cal_prob["H"] - base_prob.get("H", anchor["H"]), 4),
        "D": round(cal_prob["D"] - base_prob.get("D", anchor["D"]), 4),
        "A": round(cal_prob["A"] - base_prob.get("A", anchor["A"]), 4),
    }
    return {
        **base,
        "behavior": behavior,
        "behavior_calibrated_probabilities": cal_prob,
        "behavior_delta": delta,
        "behavior_used": True,
    }


def predict_file(jsonl_path, odds_triple=None, **kwargs):
    """CLI / 编排便捷入口: 读 jsonl 文件, 自行推导 home/away/末态 1X2 赔率, 跑融合。
    odds_triple=(h,d,a) 可显式覆盖 (否则取末 tick 1X2)。"""
    rows = []
    with open(jsonl_path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    if not rows:
        raise ValueError("空文件: %s" % jsonl_path)
    home = rows[0].get("home") or "?"
    away = rows[0].get("away") or "?"
    if odds_triple:
        oh, od, oa = odds_triple
    else:
        t = _last_1x2(rows)
        if not t:
            raise ValueError("文件无 1X2 市场: %s" % jsonl_path)
        oh, od, oa = t
    return predict_with_behavior(rows, home, away, oh, od, oa, **kwargs)


def _print(out):
    b = out["behavior"]
    cd = out["behavior_calibrated_probabilities"]
    dl = out["behavior_delta"]
    print("预测(纯锚定)      : %s  H=%.3f D=%.3f A=%.3f  conf=%.3f"
          % (out["prediction"], out["probabilities"]["H"], out["probabilities"]["D"],
             out["probabilities"]["A"], out["confidence"]))
    if not out["behavior_used"]:
        print("行为校准          : 未启用 (无有效 tick 序列)")
        return
    print("锚定(去水)        : H=%.3f D=%.3f A=%.3f" % (out["anchor_probabilities"]["H"],
          out["anchor_probabilities"]["D"], out["anchor_probabilities"]["A"]))
    print("行为校准后        : H=%.3f D=%.3f A=%.3f" % (cd["H"], cd["D"], cd["A"]))
    print("偏移(锚定→校准)   : H=%+.3f D=%+.3f A=%+.3f (pp)"
          % (dl["H"] * 100, dl["D"] * 100, dl["A"] * 100))
    best = min(("H", "D", "A"), key=lambda k: b["drift_%s" % k.lower()])
    print("行为特征          : drift(h/d/a)=%.4f/%.4f/%.4f cons=%.1f | ev=%d susp=%d margin=%.3f"
          % (b["drift_h"], b["drift_d"], b["drift_a"], b["drift_consistency"],
             b["n_events"], b["suspend_count"], b["margin_1x2"]))
    print("                   drift 看好方: %s%s" % (best, " (赔率下行=庄家看好)" if b["drift_%s" % best.lower()] < 0 else ""))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="操盘手锚定 × 定价行为规则 融合桥")
    ap.add_argument("jsonl", nargs="?", help="赔率 tick jsonl (live_poll / electronic 同构)")
    ap.add_argument("--odds", nargs=3, type=float, help="显式 1X2 赔率 h d a (否则取末 tick)")
    ap.add_argument("--demo", action="store_true", help="用内置样例 quick demo (无需文件)")
    args = ap.parse_args()

    if args.demo or not args.jsonl:
        # 内置 demo: 取一个电子盘文件演示
        fs = sorted(glob.glob(os.path.join(ROOT, "data", "electronic_poll_*.jsonl")))
        if not fs:
            print("无电子盘样例, 请提供 jsonl")
            raise SystemExit(1)
        print("[demo] 用 %s" % os.path.basename(fs[-1]))
        out = predict_file(fs[-1], odds_triple=tuple(args.odds) if args.odds else None)
        _print(out)
    else:
        out = predict_file(args.jsonl, odds_triple=tuple(args.odds) if args.odds else None)
        _print(out)
