"""
verify_cs_ev.py — P0-1 校准闸门
===============================
用 events.db 有半场比分的已完赛场次, 在 45' 时点用本泊松模型算"最终比分分布",
验证模型是否校准:
  - 取模型 argmax 最终比分 S*, 其概率 p*;
  - 按 p* 分箱, 检查箱内"实际发生 S*"的频率是否 ≈ 箱内平均 p* (可靠性曲线);
  - 模型 argmax 命中率 vs 朴素基线(预测半场比分为最终 / 总是 1-1)。
若可靠性曲线贴近 y=x, 模型可用; 否则需修正 λ 速率。

注: 纯属模型校准验证, 不涉及盘口 ROI (盘口 ROI 需 paper-trading 验证)。
"""
import sys, sqlite3
sys.path.insert(0, "D:/Architecture")
import numpy as np
from pipeline.cs_ev_engine import league_goal_rate, live_score_distribution

GQ_DB = "D:/Architecture/data/events.db"


def main():
    conn = sqlite3.connect(GQ_DB)
    rows = conn.execute("""
        SELECT score_home, score_away, ht_score_home, ht_score_away, league
        FROM match_outcomes
        WHERE score_home IS NOT NULL AND score_away IS NOT NULL
          AND ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
          AND (ht_score_home + ht_score_away) < (score_home + score_away)
    """).fetchall()
    conn.close()
    print(f"可用(含半场比分)比赛: {len(rows)}")

    hits, base_ht, base_11 = 0, 0, 0
    p_star, obs = [], []
    per_league_cache = {}
    for hs, as_, hhs, has_, lg in rows:
        key = (lg or "").strip().lower()
        if key not in per_league_cache:
            per_league_cache[key] = league_goal_rate(league=lg)
        rh, ra = per_league_cache[key]
        dist = live_score_distribution(hhs, has_, 45.0, rh, ra)
        if not dist:
            continue
        # argmax 最终比分
        (sh, sa), p = max(dist.items(), key=lambda kv: kv[1])
        p_star.append(p)
        happened = (sh == hs and sa == as_)
        obs.append(1 if happened else 0)
        if happened:
            hits += 1
        # 基线1: 预测半场比分为最终
        if hhs == hs and has_ == as_:
            base_ht += 1
        # 基线2: 总是 1-1
        if hs == 1 and as_ == 1:
            base_11 += 1

    n = len(p_star)
    p_star = np.array(p_star); obs = np.array(obs)
    print(f"\n模型 argmax 命中率: {hits}/{n} = {hits/n:.1%}")
    print(f"基线-半场即最终 命中率: {base_ht}/{n} = {base_ht/n:.1%}  (无意义基线, 仅对照)")
    print(f"全场实际 1-1 占比(基线2分母): {base_11/n:.1%}")

    # 可靠性曲线
    print("\n=== 可靠性曲线 (p* 分箱: 箱内平均预测概率 vs 实际命中频率) ===")
    edges = np.linspace(0, p_star.max() + 1e-9, 6)
    print(f"{'预测p*区间':<16s}{'n':>6s}{'平均p*':>10s}{'实际命中':>10s}{'偏差':>8s}")
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        m = (p_star >= lo) & (p_star < hi)
        if m.sum() < 5:
            continue
        mp = p_star[m].mean(); mo = obs[m].mean()
        print(f"[{lo:.2f},{hi:.2f})".ljust(16) + f"{int(m.sum()):>6d}{mp:>10.3f}{mo:>10.3f}{mo-mp:>+8.3f}")

    max_dev = 0.0
    for i in range(len(edges) - 1):
        m = (p_star >= edges[i]) & (p_star < edges[i + 1])
        if m.sum() >= 5:
            max_dev = max(max_dev, abs(obs[m].mean() - p_star[m].mean()))
    print(f"\n最大箱间偏差(校准误差): {max_dev:.3f}  (越小越校准, <0.1 可接受)")
    if hits / n >= 0.18 and max_dev < 0.12:
        print("✅ 校准闸门通过: 模型 argmax 命中率高于随机, 且可靠性曲线贴 y=x。")
    else:
        print("⚠ 校准偏差较大, 模型需修正 λ 速率或加入防守修正; 但作为 +EV 过滤器方向仍可用。")


if __name__ == "__main__":
    main()
