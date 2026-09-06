"""
验证: 滚球神器"状态感知剩余破蛋模型"的底层假设是否成立。
核心假设: 足球进球过程近似时间齐次泊松(恒定速率), 且市场去水隐含总球 λ 高度校准。

验证1 (齐次性, 纯比分): 上半场进球 ht_total vs 下半场进球 g2 是否同分布(对称)。
验证2 (条件化, 带赛前盘口): 给定赛前 λ_pre, 在 t=45 已知 ht_total,
        下半场进球 g2 = ft_total - ht_total 是否 ~ Poisson(λ_pre * 0.5)。
        同时验证运行时口径: λ_rem = λ_live - G (减法) 的合理性。
"""
import sqlite3, math, json
from collections import defaultdict

GQ = r'D:/Architecture/data/events.db'

def dewatered_over_prob(over_odds, under_odds):
    if not over_odds or not under_odds:
        return None
    try:
        p_o = 1.0 / over_odds
        p_u = 1.0 / under_odds
        s = p_o + p_u
        if s <= 0:
            return None
        return p_o / s
    except Exception:
        return None

def implied_total_from_ou(line, over_odds, under_odds):
    p = dewatered_over_prob(over_odds, under_odds)
    if p is None:
        return None
    return line + (p - 0.5)

def poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    import math
    return math.exp(-lam) * (lam ** k) / math.factorial(k)

def poisson_sf(k, lam):  # P(X >= k)
    if k <= 0:
        return 1.0
    s = 0.0
    for i in range(k):
        s += poisson_pmf(i, lam)
    return 1.0 - s

def main():
    c = sqlite3.connect(GQ)
    cur = c.cursor()

    # ── 取有半场+终比分的场 ──
    rows = cur.execute("""
        SELECT match_key, ht_score_home, ht_score_away, score_home, score_away
        FROM matches
        WHERE ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
          AND score_home IS NOT NULL AND score_away IS NOT NULL
          AND (ht_score_home + ht_score_away) < (score_home + score_away)
    """).fetchall()
    print(f"[data] 有半场+终比分场数: {len(rows)}")

    # ══ 验证1: 齐次性 (上半场 vs 下半场) ══
    ht_totals, g2_totals = [], []
    for mk, h1, a1, h2, a2 in rows:
        ht = int(h1) + int(a1)
        g2 = (int(h2) + int(a2)) - ht
        if g2 < 0:
            continue
        ht_totals.append(ht)
        g2_totals.append(g2)
    n = len(ht_totals)
    mean_ht = sum(ht_totals) / n
    mean_g2 = sum(g2_totals) / n
    print(f"\n[验证1] 齐次泊松假设")
    print(f"  上半场均值进球 = {mean_ht:.4f}  (n={n})")
    print(f"  下半场均值进球 = {mean_g2:.4f}")
    print(f"  比率 下半/上半 = {mean_g2/mean_ht:.4f} (理想~1.0, 时间各占45min)")
    # 分箱: 上半场0/1/2+ 时下半场均值
    bins = defaultdict(list)
    for ht, g2 in zip(ht_totals, g2_totals):
        key = '0' if ht==0 else ('1' if ht==1 else '2+')
        bins[key].append(g2)
    print("  半场进球桶 -> 下半场均值(应≈整体均值若齐次):")
    for k in ['0','1','2+']:
        v = bins[k]
        if v:
            print(f"    ht={k}: 下半场均值={sum(v)/len(v):.4f} (n={len(v)})")

    # ══ 验证2: 条件化 (带赛前 OU 去水 λ_pre) ══
    # 取每场赛前(最早) OU_2.50 over/under
    print(f"\n[验证2] 条件泊松: 赛前 λ_pre -> 下半场进球 g2")
    samples = []
    for mk, h1, a1, h2, a2 in rows:
        # 最早 OU_2.50 快照
        r = cur.execute("""
            SELECT odds FROM odds_snapshots
            WHERE match_key=? AND market='OU_2.50' AND selection='over'
            ORDER BY captured_at ASC LIMIT 1
        """, (mk,)).fetchone()
        if not r:
            continue
        ov = r[0]
        ru = cur.execute("""
            SELECT odds FROM odds_snapshots
            WHERE match_key=? AND market='OU_2.50' AND selection='under'
            ORDER BY captured_at ASC LIMIT 1
        """, (mk,)).fetchone()
        if not ru:
            continue
            # under 需同场同最早? 简化: 取该行
        un = ru[0]
        lam_pre = implied_total_from_ou(2.5, ov, un)
        if lam_pre is None or lam_pre <= 0:
            continue
        ht = int(h1) + int(a1)
        g2 = (int(h2)+int(a2)) - ht
        if g2 < 0:
            continue
        samples.append((lam_pre, ht, g2))

    print(f"  有效条件样本: {len(samples)}")
    if samples:
        # 口径A: λ_rem = λ_pre * 0.5 (时间缩放)
        # 口径B: λ_rem = λ_pre - ht (减法, 假设 λ_pre 是含已实现的全场预期 -- 不对, λ_pre是赛前)
        # 严格: 赛前λ_pre 对全场, 下半场占一半时间 -> λ_rem_A = λ_pre*0.5
        # 验证: 实际 g2 均值 vs λ_rem_A 均值
        lam_rem_A = [s[0]*0.5 for s in samples]
        mean_lam_A = sum(lam_rem_A)/len(lam_rem_A)
        mean_g2_v2 = sum(s[2] for s in samples)/len(samples)
        print(f"  口径A λ_rem=λ_pre*0.5 均值 = {mean_lam_A:.4f}")
        print(f"  实际下半场 g2 均值        = {mean_g2_v2:.4f}")
        print(f"  比率 g2/λ_rem_A = {mean_g2_v2/mean_lam_A:.4f} (理想~1.0)")

        # 分箱对比 (按 λ_rem_A 分5箱, 看实际 g2 频率 vs 泊松预测 P(g2>=1))
        edges = [0, 0.4, 0.8, 1.2, 1.6, 99]
        print("\n  分箱: λ_rem_A区间 | 样本数 | 实际P(g2>=1) | 泊松P(g2>=1) | 实际均值 | 泊松均值")
        for i in range(len(edges)-1):
            lo, hi = edges[i], edges[i+1]
            grp = [(lam, g2) for lam, g2 in zip(lam_rem_A, [s[2] for s in samples]) if lo <= lam < hi]
            if not grp:
                continue
            lams = [x[0] for x in grp]
            g2s = [x[1] for x in grp]
            actual_p1 = sum(1 for x in g2s if x >= 1)/len(g2s)
            actual_mean = sum(g2s)/len(g2s)
            pred_p1 = sum(poisson_sf(1, l) for l in lams)/len(lams)
            pred_mean = sum(lams)/len(lams)
            print(f"    [{lo:.1f},{hi:.1f}) | {len(grp):4d} | {actual_p1:.3f} | {pred_p1:.3f} | {actual_mean:.3f} | {pred_mean:.3f}")

    print("\n[结论] 若 下半/上半≈1.0 且 分箱实际≈泊松预测 -> 状态感知泊松条件化模型成立。")

if __name__ == '__main__':
    main()
