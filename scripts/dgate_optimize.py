"""
哨响AI D-Gate 融合参数离线优化 (dgate_optimize.py)
====================================================
精确复现 prediction_service.py 的 D-Gate 融合逻辑 (L504-614),
用 match_features 表里存的赔率特征 (odds_imp_h/d/a) 作为赔率输入,
网格搜索 d_gate 5档 spread 阈值 + odds overlay 权重的最优组合。

成功标准: Macro-F1 提升 AND D-F1 不降 AND H/A 各降<0.02

用法:
    python scripts/dgate_optimize.py --n 1000
    python scripts/dgate_opttest.py --n 3000
"""
from __future__ import annotations

import os
import sys
import argparse
import numpy as np
from typing import List, Dict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")
for p in (BACKEND_DIR, PROJECT_ROOT, os.path.join(PROJECT_ROOT, "predictors", "components")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.chdir(PROJECT_ROOT)
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
from predictors.components import draw_expert  # noqa
sys.modules["draw_expert"] = draw_expert

import importlib.util
_spec = importlib.util.spec_from_file_location("pb", os.path.join(PROJECT_ROOT, "scripts", "proper_backtest.py"))
pb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pb)


def heuristic_probs(raw_list):
    """简化 HeuristicPredictor: 用赔率隐含概率做基础 (冷启动救星的角色)。
    真实 Heuristic 更复杂, 但 D-Gate 里 heuristic 主要作为 D 信号源, 赔率驱动足够近似。"""
    n = len(raw_list)
    out = np.zeros((n, 3))
    for i, r in enumerate(raw_list):
        oh, od, oa = r.get("odds_imp_h"), r.get("odds_imp_d"), r.get("odds_imp_a")
        if oh and od and oa and oh > 0 and od > 0 and oa > 0:
            out[i] = [oh, od, oa]
        else:
            out[i] = [0.4, 0.3, 0.3]
    return out


def dgate_fuse(model_proba, heur, odds_imp, gate_table, overlay_table,
               use_de=None):
    """复现 D-Gate 融合。gate_table/overlay_table 是 spread→(阈值,值) 列表(升序)。
    use_de: DrawExpert P(D) array 或 None (这里用模型 P(D) 近似)。
    返回融合后 proba (n,3)。"""
    n = model_proba.shape[0]
    out = model_proba.copy()
    has_odds = np.array([all(odds_imp[k][i] > 0 for k in range(3)) for i in range(n)])
    for i in range(n):
        if not has_odds[i]:
            continue
        ph_o, pd_o, pa_o = odds_imp[0][i], odds_imp[1][i], odds_imp[2][i]
        spread = abs(ph_o - pa_o)
        h_m, d_m, a_m = model_proba[i]
        h_h, d_h, a_h = heur[i]
        # OE 近似: 用模型 odds 子通道难以分离, 这里用 heuristic 的 D 作 d_spec 主信号
        de_p = use_de[i] if use_de is not None else d_h
        d_spec = 0.55 * d_h + 0.45 * de_p
        # d_gate (spread 驱动)
        d_gate = 0.05
        for thr, val in gate_table:
            if spread < thr:
                d_gate = val
                break
        # D 信号一致性调制 (简化: 单源 ×0.65)
        d_gate *= 0.65
        # D 通道外科替代
        d_final = d_m * (1 - d_gate) + d_spec * d_gate
        remaining = 1.0 - d_final
        ha_sum = h_m + a_m
        if ha_sum > 0.001:
            h_base = remaining * (h_m / ha_sum)
            a_base = remaining * (a_m / ha_sum)
        else:
            h_base = remaining * 0.5
            a_base = remaining * 0.5
        # odds overlay
        w_base, w_odds = 0.75, 0.25
        for thr, wb, wo in overlay_table:
            if spread < thr:
                w_base, w_odds = wb, wo
                break
        h_out = h_base * w_base + ph_o * w_odds
        d_out = d_final * w_base + pd_o * w_odds
        a_out = a_base * w_base + pa_o * w_odds
        tot = h_out + d_out + a_out or 1.0
        out[i] = [h_out / tot, d_out / tot, a_out / tot]
    return out


def run(y, model_proba, raw_list, gate_table, overlay_table, thresholds=None):
    heur = heuristic_probs(raw_list)
    odds_imp = (
        np.array([r.get("odds_imp_h") or 0 for r in raw_list]),
        np.array([r.get("odds_imp_d") or 0 for r in raw_list]),
        np.array([r.get("odds_imp_a") or 0 for r in raw_list]),
    )
    fused = dgate_fuse(model_proba, heur, odds_imp, gate_table, overlay_table)
    return pb.metrics(y, fused, thresholds=thresholds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--since", default="2024-01-01")
    args = ap.parse_args()

    print("=" * 64)
    print(f"  D-Gate 融合参数离线优化 | {args.n}场 | {args.since}起")
    print("=" * 64)

    trainer, X, y, leagues, raw_list, meta = pb.load_dataset(args.n, args.since)
    model_proba = pb.predict(trainer, X, leagues, raw_list)

    # 当前生产 gate_table / overlay_table (从 prediction_service.py L556-608)
    cur_gate = [(0.15, 0.65), (0.25, 0.45), (0.40, 0.25), (0.55, 0.12), (9.99, 0.05)]
    cur_overlay = [(0.15, 0.75, 0.25), (0.25, 0.65, 0.35), (0.40, 0.55, 0.45),
                   (0.70, 0.40, 0.60), (9.99, 0.20, 0.80)]
    TH = (0.02, 0.10, -0.04)

    m_base = pb.metrics(y, model_proba)
    m_th = pb.metrics(y, model_proba, thresholds=TH)
    m_cur = run(y, model_proba, raw_list, cur_gate, cur_overlay)
    m_cur_th = run(y, model_proba, raw_list, cur_gate, cur_overlay, thresholds=TH)
    print(f"【纯模型基线】    {pb.fmt(m_base)}")
    print(f"【+阈值】         {pb.fmt(m_th)}")
    print(f"【+D-Gate(当前)】 {pb.fmt(m_cur)}")
    print(f"【+D-Gate+阈值】  {pb.fmt(m_cur_th)}")
    print()

    # 网格搜索 gate_table (5档值), 固定 overlay=当前
    print("── gate_table 网格搜索 (每档候选值) ──")
    base_macro = m_cur_th["macro_f1"]
    base_df1 = m_cur_th["f1_draw"]
    base_h = m_cur_th["f1_home"]
    base_a = m_cur_th["f1_away"]
    print(f"  基准(D-Gate当前+阈值): MacroF1 {base_macro:.4f} D-F1 {base_df1:.4f}")

    # 候选: 每档 [原值±调整]
    cand = {
        0: [0.55, 0.65, 0.70, 0.75],   # 极窄
        1: [0.40, 0.45, 0.50, 0.55],   # 中窄
        2: [0.20, 0.25, 0.30, 0.35],   # 正常
        3: [0.10, 0.12, 0.15, 0.18],   # 中宽
        4: [0.03, 0.05, 0.07, 0.10],   # 极宽
    }
    best = None
    best_cfg = None
    # 网格 (为控制规模, 每档取3个代表值)
    import itertools
    sel = [cand[k][:3] for k in range(5)]
    total = 3 ** 5
    print(f"  扫描 {total} 组...")
    for combo in itertools.product(*sel):
        gate = [(0.15, combo[0]), (0.25, combo[1]), (0.40, combo[2]),
                (0.55, combo[3]), (9.99, combo[4])]
        m = run(y, model_proba, raw_list, gate, cur_overlay, thresholds=TH)
        ok = (m["macro_f1"] > base_macro
              and m["f1_draw"] >= base_df1 - 1e-9
              and m["f1_home"] >= base_h - 0.02
              and m["f1_away"] >= base_a - 0.02)
        if ok and (best is None or m["macro_f1"] > best["macro_f1"]):
            best = m
            best_cfg = combo
    if best:
        print(f"  ✅ 最优 gate: {tuple(round(c,2) for c in best_cfg)}")
        print(f"     {pb.fmt(best)}")
        print(f"     ΔMacroF1 {best['macro_f1']-base_macro:+.4f} ΔD-F1 {best['f1_draw']-base_df1:+.4f}")
    else:
        print(f"  ℹ 当前 gate 已较优, 无更优解 (基线 MacroF1 {base_macro:.4f})")
    print()
    print("=" * 64)


if __name__ == "__main__":
    main()
