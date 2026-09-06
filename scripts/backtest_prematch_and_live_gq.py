# -*- coding: utf-8 -*-
"""
GQ.db 独立 OOS 回测 — 赛前分析提升量 + 滚球锚定提升量 (2026-09-02)
===================================================================
目的: 在独立大数据源 GQ.db(odds_snapshots 31M行) 上, 量化:
  (1) 赛前分析提升多少: 经验比分先验(Tier A) vs 赛前模型 predict_score(Tier B)
  (2) 滚球锚定提升多少: 赛前λ条件缩放(Tier B-cond) vs live-anchor(Tier C, α扫描)
数据源: data/GQ.db
  - match_outcomes(home,away,score_home,score_away,op_1x2_*,op_ou_*,is_virtual,is_valid) 终场+赛前盘
  - matches(mid,match_key,...) 反查 match_key
  - odds_snapshots(match_key,minute_at,score_at,market,selection,odds,line) 滚球盘
方法: 70/30 train/test; 经验先验用 train 计数; 模型为赔率派生(无参); α 在 train 学, test 验证。
"""
import os, sys, math, random, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
from pipeline.score_model import predict_score, GENERAL_OIP_GOAL_SCALE
from analysis.live_goal_probe import _current_inplay_odds

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(PROJECT_ROOT, "data", "GQ.db")
MG = 12  # 最大比分维度

def poi(k, lam):
    if k < 0 or lam <= 0:
        return 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)

def dist_from_lambdas(lh, la):
    M = [[0.0]*(MG+1) for _ in range(MG+1)]
    for h in range(MG+1):
        for a in range(MG+1):
            M[h][a] = poi(h, lh) * poi(a, la)
    s = sum(sum(r) for r in M)
    if s <= 0:
        return None
    return [[x/s for x in r] for r in M]

def cond_dist(rem_h, rem_a, H, A):
    M = [[0.0]*(MG+1) for _ in range(MG+1)]
    for h in range(H, MG+1):
        for a in range(A, MG+1):
            M[h][a] = poi(h-H, rem_h) * poi(a-A, rem_a)
    s = sum(sum(r) for r in M)
    if s <= 0:
        return None
    return [[x/s for x in r] for r in M]

def topk(M, H, A, k=3):
    cand = [(M[h][a], h, a) for h in range(H, MG+1) for a in range(A, MG+1)]
    cand.sort(reverse=True)
    return [(h, a) for _, h, a in cand[:k]]

def evaluate_prematch(subset, prior):
    """Tier A 经验先验 vs Tier B 赛前模型 (full-time 分布); prior 由训练集传入防泄漏"""
    ll_emp = ll_mod = 0.0
    t1_emp = t1_mod = t3_emp = t3_mod = 0
    n = 0
    for mk, oh, od, oa, ou, Fh, Fa in subset:
        # Tier A 经验先验
        pe = max(prior.get((Fh, Fa), 1e-6), 1e-6)
        ll_emp += -math.log(pe); n += 1
        # Tier B 赛前模型
        r = predict_score("h", "a", float(oh), float(od), float(oa),
                          goal_scale=GENERAL_OIP_GOAL_SCALE,
                          implied_total=(float(ou) if ou else None), overdispersion=0.0)
        lh = float(r.get("lh") or 1.2); la = float(r.get("la") or 0.9)
        M = dist_from_lambdas(lh, la)
        if M:
            pm = max(M[Fh][Fa], 1e-6)
            ll_mod += -math.log(pm)
            top = topk(M, 0, 0, 3)
            if (Fh, Fa) in top[:1]: t1_mod += 1
            if (Fh, Fa) in top[:3]: t3_mod += 1
        # 经验 top
        emp_top = sorted(prior.items(), key=lambda kv: -kv[1])[:3]
        emp_topk = [k for k, _ in emp_top]
        if (Fh, Fa) in emp_topk[:1]: t1_emp += 1
        if (Fh, Fa) in emp_topk[:3]: t3_emp += 1
    n = max(n, 1)
    return (ll_emp/n, t1_emp/n, t3_emp/n), (ll_mod/n, t1_mod/n, t3_mod/n)

def rem_lambda(x2, ou_line, Hh, Ah, alpha):
    r_raw = predict_score("h", "a", float(x2[0]), float(x2[1]), float(x2[2]),
                          goal_scale=GENERAL_OIP_GOAL_SCALE, implied_total=None, overdispersion=0.0)
    lh_r = float(r_raw.get("lh") or 1.0); la_r = float(r_raw.get("la") or 1.0)
    split_raw = (la_r/(lh_r+la_r)) if (lh_r+la_r) > 0 else 0.5
    r_ou = predict_score("h", "a", float(x2[0]), float(x2[1]), float(x2[2]),
                         goal_scale=GENERAL_OIP_GOAL_SCALE, implied_total=float(ou_line), overdispersion=0.0)
    lh_o = float(r_ou.get("lh") or 1.0); la_o = float(r_ou.get("la") or 1.0)
    split_ou = (la_o/(lh_o+la_o)) if (lh_o+la_o) > 0 else 0.5
    split = alpha*split_raw + (1-alpha)*split_ou
    rem_total = max(0.1, float(ou_line) - (Hh+Ah))
    return max(0.01, rem_total*(1-split)), max(0.01, rem_total*split)

def evaluate_inplay(subset, alpha, use_live):
    ll = t1 = t3 = 0; n = 0
    for mk, snap, prem, Fh, Fa in subset:
        Hh, Ah = snap["Hh"], snap["Ah"]
        if use_live:
            rh, ra = rem_lambda(snap["x2"], snap["ou"][0], Hh, Ah, alpha)
        else:
            r = predict_score("h", "a", float(prem[0]), float(prem[1]), float(prem[2]),
                              goal_scale=GENERAL_OIP_GOAL_SCALE, implied_total=None, overdispersion=0.0)
            lh, la = float(r.get("lh") or 1.2), float(r.get("la") or 0.9)
            T = 0.5
            rh, ra = max(0.01, lh*T), max(0.01, la*T)
        M = cond_dist(rh, ra, Hh, Ah)
        if not M:
            n += 1; continue
        p = max(M[Fh][Fa], 1e-6); ll += -math.log(p); n += 1
        top = topk(M, Hh, Ah, 3)
        if (Fh, Fa) in top[:1]: t1 += 1
        if (Fh, Fa) in top[:3]: t3 += 1
    n = max(n, 1)
    return ll/n, t1/n, t3/n

def main():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    # 全量赛前评估集
    rows = con.execute("""
        SELECT mo.home, mo.away, mo.score_home, mo.score_away,
               mo.op_1x2_h, mo.op_1x2_d, mo.op_1x2_a, mo.op_ou_line,
               m.match_key
        FROM match_outcomes mo JOIN matches m ON mo.mid=m.mid
        WHERE mo.is_virtual=0 AND mo.is_valid=1
          AND mo.score_home IS NOT NULL AND mo.score_away IS NOT NULL
          AND mo.op_1x2_h>0 AND mo.op_1x2_d>0 AND mo.op_1x2_a>0
    """).fetchall()
    prem_set = []
    for r in rows:
        prem_set.append((r["match_key"],
                         r["op_1x2_h"], r["op_1x2_d"], r["op_1x2_a"],
                         r["op_ou_line"], int(r["score_home"]), int(r["score_away"])))
    # 滚球子集(有半场快照)
    live_set = []
    for mk, oh, od, oa, ou, Fh, Fa in prem_set:
        snap = con.execute("""
            SELECT minute_at, score_at FROM odds_snapshots
            WHERE match_key=? AND minute_at BETWEEN 40 AND 50 AND score_at IS NOT NULL
            ORDER BY abs(minute_at-45) ASC LIMIT 1
        """, (mk,)).fetchone()
        if not snap:
            continue
        try:
            Hh, Ah = (int(x) for x in str(snap["score_at"]).split("-"))
        except Exception:
            continue
        cur = _current_inplay_odds(con, mk, snap["minute_at"])
        if not cur or not cur.get("x2") or not cur.get("ou"):
            continue
        live_set.append((mk, {"Hh": Hh, "Ah": Ah, "x2": cur["x2"], "ou": cur["ou"]},
                         (oh, od, oa), Fh, Fa))
    con.close()
    print(f"[GQ] 赛前评估集 n={len(prem_set)} | 滚球子集 n={len(live_set)}")

    random.seed(42)
    random.shuffle(prem_set); random.shuffle(live_set)
    ptrain, ptest = prem_set[:int(len(prem_set)*0.7)], prem_set[int(len(prem_set)*0.7):]
    ltrain, ltest = live_set[:int(len(live_set)*0.7)], live_set[int(len(live_set)*0.7):]

    # ---- 赛前分析提升量 ----
    # 经验先验 from 训练集(防泄漏)
    prior = {}
    for _, _, _, _, _, Fh, Fa in ptrain:
        prior[(Fh, Fa)] = prior.get((Fh, Fa), 0) + 1
    Np = sum(prior.values()); prior = {k: v/Np for k, v in prior.items()}
    (ea_ll, ea_t1, ea_t3), (eb_ll, eb_t1, eb_t3) = evaluate_prematch(ptest, prior)
    print(f"\n[赛前 TEST OOS] 经验先验   logloss={ea_ll:.4f} top1={ea_t1*100:.1f}% top3={ea_t3*100:.1f}%")
    print(f"[赛前 TEST OOS] 赛前模型   logloss={eb_ll:.4f} top1={eb_t1*100:.1f}% top3={eb_t3*100:.1f}%")
    print(f"  赛前分析提升: Δlogloss={eb_ll-ea_ll:+.4f} | top1 Δ={(eb_t1-ea_t1)*100:+.1f}pp | top3 Δ={(eb_t3-ea_t3)*100:+.1f}pp")

    # ---- 滚球锚定提升量 (α扫描 on train) ----
    best_a, best_ll = 0.0, 1e9
    print("\n[滚球 α扫描 train]")
    for a in (0.0, 0.25, 0.5, 0.75, 1.0):
        ll, t1, t3 = evaluate_inplay(ltrain, a, True)
        print(f"  alpha={a:.2f} logloss={ll:.4f} top1={t1*100:.1f}% top3={t3*100:.1f}%")
        if ll < best_ll:
            best_ll, best_a = ll, a
    bl_ll, bl_t1, bl_t3 = evaluate_inplay(ltest, 0.0, False)
    la_ll, la_t1, la_t3 = evaluate_inplay(ltest, best_a, True)
    print(f"\n[滚球 TEST OOS] 赛前λ条件缩放 logloss={bl_ll:.4f} top1={bl_t1*100:.1f}% top3={bl_t3*100:.1f}%")
    print(f"[滚球 TEST OOS] live-anchor α={best_a:.2f} logloss={la_ll:.4f} top1={la_t1*100:.1f}% top3={la_t3*100:.1f}%")
    print(f"  滚球锚定提升: Δlogloss={la_ll-bl_ll:+.4f} | top1 Δ={(la_t1-bl_t1)*100:+.1f}pp | top3 Δ={(la_t3-bl_t3)*100:+.1f}pp")

    return {"gq_prematch_n": len(prem_set), "gq_live_n": len(live_set),
            "prematch": {"empirical": [ea_ll, ea_t1, ea_t3], "model": [eb_ll, eb_t1, eb_t3]},
            "live": {"baseline": [bl_ll, bl_t1, bl_t3], "anchor": [la_ll, la_t1, la_t3], "best_alpha": best_a}}

if __name__ == "__main__":
    res = main()
    print("\nJSON:", json.dumps(res, ensure_ascii=False))
