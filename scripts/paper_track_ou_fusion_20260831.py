"""OU 融合 受控纸盘(paper) harness (2026-08-31)
================================================================
受控前向跟踪 OU 融合信号, 阈值 m>=EDGE_MIN, 监控段间衰减(IR-30 诚实边界)。
- 纸盘宇宙 = 训练 cutoff(早70%切分点) 之后的干净完结场(目前=778 OOS; 未来新比赛入库重跑自动扩展)。
- 仅当 model P(over) - 开盘去水隐含P(over) >= EDGE_MIN 才下注(over/under), 平注1单位。
- 按 kickoff 切 Q 段时间窗, 逐段 ROI; 末段<首段 或 末段<0 → decay_flag 亮红。
- 输出: 纸盘日志 CSV(追加式) + 汇总 JSON + 打印。
绝不产生真实下注(IR-21 建仓须人工审批); 纯研究/监控。
"""
from __future__ import annotations
import os, sys, json, csv, datetime
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.build_fused_models_20260831 import collect, fl_probs, p_over, FIT_FRAC
import joblib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
REPORTS = os.path.join(ROOT, "reports")
f_ou = joblib.load(os.path.join(MODELS, "fused_ou_20260831.joblib"))

EDGE_MIN = 0.02      # 最小 edge 才下注
Q = 5                # 时间分段数

def fused_ou_pover(a, b):
    if a is None:
        return float(b)  # fl_model_ou 已下线(2026-08-31): 纯泊松回退
    return float(f_ou["meta"].predict_proba(np.array([[a, b]]))[0, 1])

def implied_ou(ov, un):
    return (1/ov)/((1/ov)+(1/un))

def main():
    recs = collect()
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    cutoff = recs[k-1]["ko"]                 # 训练集最晚 kickoff
    paper = [r for r in recs if r["ko"] > cutoff]   # 训练后 = 前向宇宙
    paper.sort(key=lambda r: r["ko"])
    print(f"训练 cutoff kickoff = {cutoff}")
    print(f"纸盘宇宙(训练后干净场) n = {len(paper)}")

    rows = []
    cum = 0.0
    for r in paper:
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        fl_ou_pover = fl["ou"][0] if fl["ou"] else None  # fl_model_ou 已下线(2026-08-31) → None 回退纯泊松
        po = float(p_over(r["lam"][0], r["lam"][1], r["line"]))
        model_p = fused_ou_pover(fl_ou_pover, po)
        imp = implied_ou(r["ov"], r["un"])
        tot = r["sh"] + r["sa"]
        y = 1 if tot > r["line"] else 0
        edge = model_p - imp
        bet = None; side = ""; odds = 0.0; profit = None
        if abs(edge) >= EDGE_MIN:   # 模型与市场分歧 >=2% 才下注(双边对称)
            if edge > 0:
                side, odds = "over", r["ov"]
            else:
                side, odds = "under", r["un"]
            bet = True
            profit = (odds - 1) if ((side == "over" and y == 1) or (side == "under" and y == 0)) else -1.0
            cum += profit
        rows.append(dict(
            ko=r["ko"], league=r["league"], line=r["line"],
            implied=round(imp, 4), model_p=round(model_p, 4), edge=round(edge, 4),
            side=side, odds=round(odds, 3) if odds else "", y=y,
            profit=round(profit, 3) if profit is not None else "",
        ))

    bets = [x for x in rows if x["side"]]
    n_bets = len(bets)
    roi = float(np.mean([x["profit"] for x in bets])) if bets else float("nan")
    cum_roi = cum / n_bets if n_bets else float("nan")

    # Bootstrap 95% CI (选择性策略 ROI, IR-30 显著性)
    roi_ci = [None, None]
    if n_bets >= 30:
        prof = np.array([float(x["profit"]) for x in bets])
        rng = np.random.default_rng(20260831)
        bs = np.array([prof[rng.integers(0, n_bets, n_bets)].mean() for _ in range(1000)])
        roi_ci = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]

    # 时间分段(按 kickoff 排序的 bets)
    bets_sorted = sorted(bets, key=lambda x: x["ko"])
    segs = np.array_split(np.array(bets_sorted, dtype=object), Q) if n_bets >= Q else [np.array(bets_sorted, dtype=object)]
    seg_roi = []
    for s in segs:
        if len(s) == 0:
            seg_roi.append(None)
        else:
            seg_roi.append(float(np.mean([float(x["profit"]) for x in s])))
    decay_flag = False
    valid = [x for x in seg_roi if x is not None]
    if len(valid) >= 2:
        med = float(np.median(valid))
        # 真实衰减: 末段转负, 或跌到中位数的半数以下(前向崩塌预警)
        if valid[-1] < 0 or valid[-1] < 0.5 * med:
            decay_flag = True

    # 写 CSV(追加式: 若存在旧文件则保留历史? 这里数据确定, 覆盖式重算, 标注 generated_at)
    csv_path = os.path.join(REPORTS, "paper_track_ou_fusion.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ko","league","line","implied","model_p","edge","side","odds","y","profit"])
        w.writeheader()
        for x in rows:
            w.writerow(x)

    summary = dict(
        generated_at=datetime.datetime.now().astimezone().isoformat(),
        edge_min=EDGE_MIN, cutoff_kickoff=cutoff,
        paper_universe_n=len(paper), bets_placed=n_bets,
        roi_per_bet=roi, cumulative_roi=cum_roi,
        roi_bootstrap_ci=[round(v, 4) if v is not None else None for v in roi_ci],
        segment_roi=[None if v is None else round(v, 4) for v in seg_roi],
        decay_flag=decay_flag,
        note="纯纸盘(无真实下注); 阈值 m>=0.02; 训练后OOS前向监控; 新数据入库重跑即扩展",
    )
    with open(os.path.join(REPORTS, "paper_track_ou_fusion_summary_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n[纸盘] edge阈值={EDGE_MIN} | 下注 {n_bets}/{len(paper)} 场 | 单注ROI={roi:+.2%} | 累计ROI={cum_roi:+.2%}")
    print(f"[时间分段 ROI] " + " | ".join(f"段{i+1}:{('—' if v is None else f'{v:+.2%}')}" for i, v in enumerate(seg_roi)))
    print(f"[衰减监控] {'🔴 衰减告警(decay_flag)' if decay_flag else '🟢 未检出衰减'}")
    print(f"-> {csv_path} + paper_track_ou_fusion_summary_20260831.json")

if __name__ == "__main__":
    main()
