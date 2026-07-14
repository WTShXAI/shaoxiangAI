#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G3 · 分歧子集样本增厚 + bootstrap 置信区间.

目的: 回答"分歧闸门下 value_layer 选边策略, 当前仅 139 注, 统计上够不够放开半自动?"
方法:
  - 复用 p0_3 的遍历逻辑(analyze_multi + compute_value_layer + 规范半凯利 + 10%封顶),
    但捕获「分歧子集且 value_layer decision==BET」每注的 对数收益 g_i = log(1 + pnl/equity_before).
  - bootstrap: 对 g_i 重采样(B=20000), 得累积 ROI = exp(n*mean_g*) - 1 的经验分布, 取 95% CI.
  - 样本增长投影: 把 g_i 重采样到 N=139/250/500/1000, 看 CI 如何随 n 收紧(直观展示"增厚到 n≥500"的门槛意义).
  - 同时给出: 胜率 / 平均注码占比 / 平均单边 edge, 作为策略健康度指标.
数据源/风控同 P0-2/P0-3: odds_features 双庄同场 16,140 场, 虚拟本金3000, 时序OOS.
"""
import sqlite3
import sys
import math
import json
import random

sys.path.insert(0, "D:/Architecture")

from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
from pipeline.deep_report import compute_value_layer, consensus_probs, kelly_fraction

DB = "data/football_data.db"
BANKROLL = 3000.0
FRAC_KELLY = 0.5
MAX_STAKE_FRAC = 0.10
OUT_JSON = "deliverables/disagreement_sample_growth.json"
IDX = {"H": 0, "D": 1, "A": 2}
B = 20000  # bootstrap 次数


def fetch_pairs():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    q = """SELECT w.home_team, w.away_team, w.match_date,
                  w.close_h, w.close_d, w.close_a, w.home_score, w.away_score,
                  i.close_h, i.close_d, i.close_a
           FROM odds_features w JOIN odds_features i
             ON w.home_team=i.home_team AND w.away_team=i.away_team AND w.match_date=i.match_date
           WHERE w.source='william_hill' AND i.source='interwetten'
             AND w.close_h>1 AND i.close_h>1
             AND w.home_score IS NOT NULL AND i.home_score IS NOT NULL
           ORDER BY w.match_date"""
    rows = cur.execute(q).fetchall()
    con.close()
    return rows


def winner_of(hs, as_):
    if hs > as_:
        return "H"
    if as_ > hs:
        return "A"
    return "D"


def decide_dir_capture(direction_idx, p_vec, odds, equity, winner):
    """按指定方向下注(价值层信号): 规范半凯利(封顶). 返回(新equity, stake, win, pnl)."""
    p = p_vec[direction_idx]
    o = odds[direction_idx]
    k = kelly_fraction(p, o)
    if k <= 0:
        return equity, 0.0, False, 0.0
    frac = FRAC_KELLY * k
    if frac > MAX_STAKE_FRAC:
        frac = MAX_STAKE_FRAC
    stake = frac * equity
    if stake <= 0 or stake > equity:
        return equity, 0.0, False, 0.0
    d = ("H", "D", "A")[direction_idx]
    if winner == d:
        equity += stake * (o - 1)
        pnl = stake * (o - 1)
        win = True
    else:
        equity -= stake
        pnl = -stake
        win = False
    return equity, stake, win, pnl


def collect_disagreement_logreturns(pairs):
    """遍历, 捕获分歧子集 gated value_layer 策略每注的对数收益 g_i=log(1+pnl/equity_before)."""
    eng = ReverseOddsEngine()
    g_list = []          # 对数收益
    stakes = []          # 注码占比(stake/equity_before)
    wins = 0
    equity = BANKROLL
    n_total_pairs = 0
    n_disagree = 0
    for (ht, at, md, wh_h, wh_d, wh_a, hs, as_, iw_h, iw_d, iw_a) in pairs:
        wh = [wh_h, wh_d, wh_a]
        iw = [iw_h, iw_d, iw_a]
        try:
            b1 = OddsInput(open_h=wh[0], open_d=wh[1], open_a=wh[2],
                           close_h=wh[0], close_d=wh[1], close_a=wh[2])
            b2 = OddsInput(open_h=iw[0], open_d=iw[1], open_a=iw[2],
                           close_h=iw[0], close_d=iw[1], close_a=iw[2])
        except Exception:
            continue
        n_total_pairs += 1
        res = eng.analyze_multi([b1, b2])
        if not res.disagreement_detected:
            continue
        n_disagree += 1
        cons = consensus_probs([wh, iw])
        best_odds = [max(wh[0], iw[0]), max(wh[1], iw[1]), max(wh[2], iw[2])]
        w = winner_of(hs, as_)
        vl = compute_value_layer(odds=best_odds, model_probs=cons,
                                 bankroll=BANKROLL, frac_kelly=FRAC_KELLY)
        if vl["decision"] != "BET":
            continue
        di = IDX[vl["best_direction"]]
        eq_before = equity
        equity, stake, win, pnl = decide_dir_capture(di, cons, best_odds, equity, w)
        if stake <= 0:
            continue
        g = math.log(1.0 + pnl / eq_before)   # 对数收益(可加性)
        g_list.append(g)
        stakes.append(stake / eq_before)
        if win:
            wins += 1
    return {
        "n_total_pairs": n_total_pairs,
        "n_disagree": n_disagree,
        "n_bets": len(g_list),
        "wins": wins,
        "g_list": g_list,
        "avg_stake_frac": (sum(stakes) / len(stakes)) if stakes else 0.0,
        "final_equity": equity,
    }


def bootstrap_ci(g_list, n_target, B=B):
    """把 g_list 重采样到长度 n_target, 计算累积 ROI=exp(n*mean_g*)-1 的 95% CI."""
    rng = random.Random(20260711)
    n = len(g_list)
    rois = []
    for _ in range(B):
        sample = [g_list[rng.randrange(n)] for _ in range(n_target)]
        mean_g = sum(sample) / n_target
        roi = math.exp(n_target * mean_g) - 1.0
        rois.append(roi * 100.0)
    rois.sort()
    lo = rois[int(0.025 * B)]
    hi = rois[int(0.975 * B)]
    med = rois[int(0.5 * B)]
    return {"n": n_target, "roi_median": med, "ci_low": lo, "ci_high": hi}


def main():
    pairs = fetch_pairs()
    d = collect_disagreement_logreturns(pairs)
    n_bets = d["n_bets"]
    if n_bets == 0:
        print("ERROR: 分歧子集无下注, 检查 analyze_multi/compute_value_layer 调用")
        sys.exit(1)

    mean_g = sum(d["g_list"]) / n_bets
    roi_point = (math.exp(n_bets * mean_g) - 1.0) * 100.0
    win_rate = d["wins"] / n_bets * 100.0

    # 当前 n 的 bootstrap CI
    cur = bootstrap_ci(d["g_list"], n_bets)
    # 样本增长投影
    projections = []
    for N in (139, 250, 500, 1000):
        if N < n_bets:
            N = n_bets
        projections.append(bootstrap_ci(d["g_list"], N))

    out = {
        "meta": {
            "bankroll": BANKROLL, "frac_kelly": FRAC_KELLY,
            "max_stake_frac": MAX_STAKE_FRAC, "bootstrap_B": B,
            "source": "odds_features 双庄同场(william_hill×interwetten)",
        },
        "summary": {
            "n_total_pairs": d["n_total_pairs"],
            "n_disagree_pairs": d["n_disagree"],
            "n_bets_disagreement_gated": n_bets,
            "wins": d["wins"],
            "win_rate_pct": round(win_rate, 2),
            "avg_stake_frac_pct": round(d["avg_stake_frac"] * 100, 2),
            "final_equity": round(d["final_equity"], 1),
            "roi_point_estimate_pct": round(roi_point, 2),
        },
        "ci_current_n": cur,
        "sample_growth_projection": projections,
        "verdict": None,
    }

    # 诚实 verdict
    ci_lo = cur["ci_low"]
    n500 = next((p for p in projections if p["n"] == 500), None)
    if n_bets >= 500 and ci_lo > 0:
        verdict = (f"✅ 分歧子集 n={n_bets}≥500 且 95%CI 下限 {ci_lo:+.1f}%>0 → "
                   f"具备放开半自动的数据基础(仍需 G2 9111 灰度冒烟 + 真 RLM/drift 升级).")
    elif n500 and n500["ci_low"] > 0:
        verdict = (f"⏳ 当前 n={n_bets} 不足; 若样本增厚到 n=500(保持当前 edge), 95%CI 下限预计 "
                   f"{n500['ci_low']:+.1f}%>0 → 届时具备放开半自动数据基础. 当前需靠 live 累积(G2)+历史不动.")
    else:
        verdict = (f"⚠️ 即使到 n=500, 95%CI 下限 {n500['ci_low']:+.1f}% 仍≤0 → 当前 edge 不够稳, "
                   f"需同时提升策略质量(如 G4 RLM 真源 / G5 booster)而非仅靠堆样本.")
    out["verdict"] = verdict

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # 打印
    print(f"[G3] 双庄同场总数={d['n_total_pairs']}  分歧对={d['n_disagree']}  "
          f"分歧闸门下下注={n_bets}注  胜率={win_rate:.1f}%")
    print(f"[G3] 点估计 ROI={roi_point:+.1f}%  平均注码占比={d['avg_stake_frac']*100:.1f}%  "
          f"终值本金={d['final_equity']:.0f}")
    print(f"[G3] 当前 n={n_bets} bootstrap 95%CI = [{ci_lo:+.1f}%, {cur['ci_high']:+.1f}%]")
    for p in projections:
        print(f"     投影 n={p['n']:>4}: ROI中位 {p['roi_median']:+.1f}%  "
              f"95%CI [{p['ci_low']:+.1f}%, {p['ci_high']:+.1f}%]")
    print(f"[G3] verdict: {verdict}")
    print(f"[G3] 写出 -> {OUT_JSON}")


if __name__ == "__main__":
    main()
