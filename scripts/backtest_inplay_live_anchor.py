# -*- coding: utf-8 -*-
"""
in-play 实时滚球盘重新锚定 — OOS 回测 + split 混合系数学习 (2026-09-02)
============================================================================
目的: 验证"在滚球状态用真实滚球盘(1X2/OU/AH)重锚剩余 λ"是否系统性提升
      终场比分预测, 并学习最优 split 混合系数 alpha。

数据: events.db
  - odds_snapshots(match_key, minute_at, score_at, market, selection, odds, line)
      半场快照(40<=minute_at<=50, score_at 非空) → 真实滚球盘 + 半场比分
  - match_outcomes(mid, score_home, score_away, op_1x2_h/d/a) + matches(mid, score_missing)
      终场比分(非虚拟, score_missing=0) + 赛前 1X2

方法(每场, 半场比分 Hh-Ah, 终场 Fh-Fa):
  baseline : 赛前λ(solve_oip on op_1x2) × T_ratio=(90-45)/90=0.5 → 条件Poisson
  v1(raw)  : 剩余λ split=live 1X2 原始 solve_oip split; total=live_OU-(Hh+Ah)
  v2(ou)   : 剩余λ split=live 1X2 + implied_total=live_OU 的 solve_oip split; total同上
  blend(a) : split = a*split_raw + (1-a)*split_ou ; total同上
  条件Poisson: Mc[h,a]=poi(h-Hh,rem_h)*poi(a-Ah,rem_a), 归一化 h>=Hh,a>=Ah

指标: 每场 logloss=-log Mc[Fh,Fa]; top1/top3 命中率。alpha 在 train(70%) 学习, test(30%) 验证。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3, math, random, json
from pipeline.score_model import predict_score, GENERAL_OIP_GOAL_SCALE

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(PROJECT_ROOT, "data", "events.db")

def poi(k, lam):
    if k < 0 or lam <= 0:
        return 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)

def cond_dist(rem_h, rem_a, H, A, mg=12):
    M = [[0.0]*(mg+1) for _ in range(mg+1)]
    for h in range(H, mg+1):
        for a in range(A, mg+1):
            M[h][a] = poi(h-H, rem_h) * poi(a-A, rem_a)
    s = sum(sum(r) for r in M)
    if s <= 0:
        return None
    return [[x/s for x in r] for r in M]

def top_scores(M, H, A, k=3):
    cand = []
    for h in range(H, len(M)):
        for a in range(A, len(M[0])):
            cand.append((M[h][a], h, a))
    cand.sort(reverse=True)
    return cand[:k]

def load_matches(con):
    rows = con.execute("""
        SELECT m.match_key, mo.score_home, mo.score_away,
               mo.op_1x2_h, mo.op_1x2_d, mo.op_1x2_a
        FROM matches m JOIN match_outcomes mo ON m.mid=mo.mid
        WHERE m.score_missing=0 AND (mo.is_virtual IS NULL OR mo.is_virtual=0)
          AND m.match_key IN (
            SELECT DISTINCT match_key FROM odds_snapshots
            WHERE minute_at BETWEEN 40 AND 50 AND score_at IS NOT NULL)
    """).fetchall()
    return rows

def halftime_snapshot(con, match_key):
    # 取 40-50 分钟内 score_at 非空、分钟最接近 45 的一条, 读半场比分 + 实时盘
    rows = con.execute("""
        SELECT minute_at, score_at FROM odds_snapshots
        WHERE match_key=? AND minute_at BETWEEN 40 AND 50 AND score_at IS NOT NULL
        ORDER BY abs(minute_at-45) ASC LIMIT 1
    """, (match_key,)).fetchone()
    if not rows:
        return None
    minute, score_at = rows["minute_at"], rows["score_at"]
    try:
        Hh, Ah = (int(x) for x in str(score_at).split("-"))
    except Exception:
        return None
    from analysis.live_goal_probe import _current_inplay_odds, _current_inplay_ah_odds
    cur = _current_inplay_odds(con, match_key, minute)
    ah = _current_inplay_ah_odds(con, match_key, minute)
    if not cur or not cur.get("x2") or not cur.get("ou"):
        return None
    return {"minute": minute, "Hh": Hh, "Ah": Ah,
            "x2": cur["x2"], "ou": cur["ou"], "ah": ah}

def rem_lambda(x2, ou_line, Hh, Ah, alpha):
    # 原始 1X2 split
    r_raw = predict_score("h","a", float(x2[0]), float(x2[1]), float(x2[2]),
                          goal_scale=GENERAL_OIP_GOAL_SCALE, implied_total=None, overdispersion=0.0)
    lh_r = float(r_raw.get("lh") or 1.0); la_r = float(r_raw.get("la") or 1.0)
    split_raw = (la_r/(lh_r+la_r)) if (lh_r+la_r) > 0 else 0.5
    # OU 锚定 split
    r_ou = predict_score("h","a", float(x2[0]), float(x2[1]), float(x2[2]),
                         goal_scale=GENERAL_OIP_GOAL_SCALE, implied_total=float(ou_line), overdispersion=0.0)
    lh_o = float(r_ou.get("lh") or 1.0); la_o = float(r_ou.get("la") or 1.0)
    split_ou = (la_o/(lh_o+la_o)) if (lh_o+la_o) > 0 else 0.5
    split = alpha*split_raw + (1-alpha)*split_ou
    rem_total = max(0.1, float(ou_line) - (Hh+Ah))
    return max(0.01, rem_total*(1-split)), max(0.01, rem_total*split)

def main():
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    matches = load_matches(con)
    random.seed(42)
    data = []
    for m in matches:
        mk = m["match_key"]
        snap = halftime_snapshot(con, mk)
        if not snap:
            continue
        # 赛前 odds (baseline)
        oh, od, oa = m["op_1x2_h"], m["op_1x2_d"], m["op_1x2_a"]
        if not (oh and od and oa and oh>0):
            continue
        Fh, Fa = int(m["score_home"]), int(m["score_away"])
        data.append((mk, snap, (oh,od,oa), Fh, Fa))
    con.close()
    print(f"可回测比赛 = {len(data)}")

    # 切分
    random.shuffle(data)
    n_train = int(len(data)*0.7)
    train, test = data[:n_train], data[n_train:]

    def evaluate(subset, alpha, use_live):
        ll_sum, n, t1, t3 = 0.0, 0, 0, 0
        for mk, snap, prem, Fh, Fa in subset:
            Hh, Ah = snap["Hh"], snap["Ah"]
            if use_live:
                rh, ra = rem_lambda(snap["x2"], snap["ou"][0], Hh, Ah, alpha)
            else:
                r = predict_score("h","a", float(prem[0]), float(prem[1]), float(prem[2]),
                                   goal_scale=GENERAL_OIP_GOAL_SCALE, implied_total=None, overdispersion=0.0)
                lh, la = float(r.get("lh") or 1.2), float(r.get("la") or 0.9)
                T = 0.5
                rh, ra = max(0.01, lh*T), max(0.01, la*T)
            M = cond_dist(rh, ra, Hh, Ah)
            if not M:
                n += 1; continue
            p = max(M[Fh][Fa], 1e-6)
            ll_sum += -math.log(p); n += 1
            top = top_scores(M, Hh, Ah, 3)
            tops = [(h,a) for _,h,a in top]
            if (Fh,Fa) in tops[:1]: t1 += 1
            if (Fh,Fa) in tops[:3]: t3 += 1
        return ll_sum/max(n,1), t1/max(n,1), t3/max(n,1), n

    # baseline
    b_ll, b_t1, b_t3, _ = evaluate(train, 0.0, False)
    print(f"\n[BASELINE 赛前λ缩放] train logloss={b_ll:.4f} top1={b_t1*100:.1f}% top3={b_t3*100:.1f}%")
    # alpha 扫描 (train 学习)
    best_a, best_ll = 0.0, 1e9
    print("\nalpha 扫描 (train):")
    for a in (0.0, 0.25, 0.5, 0.75, 1.0):
        ll, t1, t3, n = evaluate(train, a, True)
        print(f"  alpha={a:.2f} logloss={ll:.4f} top1={t1*100:.1f}% top3={t3*100:.1f}%")
        if ll < best_ll:
            best_ll, best_a = ll, a
    # test 验证 (最优 alpha + baseline)
    bl_ll, bl_t1, bl_t3, _ = evaluate(test, 0.0, False)
    la_ll, la_t1, la_t3, _ = evaluate(test, best_a, True)
    print(f"\n[TEST OOS] baseline logloss={bl_ll:.4f} top1={bl_t1*100:.1f}% top3={bl_t3*100:.1f}%")
    print(f"[TEST OOS] live-anchor(alpha={best_a:.2f}) logloss={la_ll:.4f} top1={la_t1*100:.1f}% top3={la_t3*100:.1f}%")
    print(f"  Δlogloss = {la_ll-bl_ll:+.4f} (负=校准改善) | top1 Δ={(la_t1-bl_t1)*100:+.1f}pp | top3 Δ={(la_t3-bl_t3)*100:+.1f}pp")
    # 最佳 alpha 全局复算(报告)
    g_ll, g_t1, g_t3, _ = evaluate(data, best_a, True)
    print(f"\n[全局 live-anchor alpha={best_a:.2f}] logloss={g_ll:.4f} top1={g_t1*100:.1f}% top3={g_t3*100:.1f}%")
    return {"n": len(data), "best_alpha": best_a,
            "baseline_test": {"logloss": bl_ll, "top1": bl_t1, "top3": bl_t3},
            "live_test": {"logloss": la_ll, "top1": la_t1, "top3": la_t3}}

if __name__ == "__main__":
    res = main()
    print("\nJSON:", json.dumps(res, ensure_ascii=False))
