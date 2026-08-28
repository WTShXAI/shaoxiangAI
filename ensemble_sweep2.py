# -*- coding: utf-8 -*-
"""
混合扫描 · 直接回答「合并能提升多少」
=====================================
对真实组件做凸组合: B = w*本系统 + (1-w)*去水基线
- 静态段: WI 教师 vs 去水 (测「赛前合并」空间)
- 滚球段: live_1x2 模型 vs 去水 (测「合并补回准确率」空间)
输出每档权重的 AUC/Acc/LogLoss/Brier, 找最优 w。
"""
import os, json
import numpy as np
import unified_corrected_duel as U

def blend_sweep(label, rows, sys_fn, nav_fn):
    y = np.array([x["y"] for x in rows])
    S = []; N = []
    for x in rows:
        sp = sys_fn(x); S.append(sp if sp else [1/3,1/3,1/3])
        N.append(nav_fn(x))
    S = np.array(S, dtype=float); N = np.array(N, dtype=float)
    print(f"\n===== {label}  (n={len(rows)}) =====")
    print(f"{'w(本系统)':>10}{'AUC':>9}{'Acc':>8}{'LogLoss':>10}{'Brier':>9}")
    print("-"*48)
    out = []
    for w in [0.0,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0]:
        B = w*S + (1-w)*N
        B = B / B.sum(axis=1, keepdims=True)
        r = U.metrics(y, B)
        print(f"{w:>10.1f}{r['auc']:>9.4f}{r['acc']:>8.3f}{r['logloss']:>10.4f}{r['brier']:>9.4f}")
        out.append(dict(w=w, auc=round(r['auc'],4), acc=round(r['acc'],4),
                        logloss=round(r['logloss'],4), brier=round(r['brier'],4)))
    return out

def main():
    # 静态: WI 教师
    sr = U.fetch_static()
    static = blend_sweep("STATIC · WI教师(w) + 去水(1-w)", sr,
        lambda x: U.sys_static(x["oh"],x["od"],x["oa"],x["ooh"],x["ood"],x["ooa"]),
        lambda x: U.naive_probs(x["oh"],x["od"],x["oa"]))
    # 滚球: live_1x2 模型
    ir = U.fetch_inplay()
    inplay = blend_sweep("IN-PLAY · live模型(w) + 去水(1-w)", ir,
        lambda x: U.sys_inplay(x["h"],x["d"],x["a"],x["sh"],x["sa"],x["minute"]),
        lambda x: U.naive_probs(x["h"],x["d"],x["a"]))

    outp = os.path.join(U.ROOT, "deliverables", "ensemble_sweep2_result.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(dict(static=static, inplay=inplay), f, ensure_ascii=False, indent=2)
    print(f"\n[ok] -> {outp}")

if __name__ == "__main__":
    main()
