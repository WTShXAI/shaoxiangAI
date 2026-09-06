#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
form_heuristic_study.py — 验证用户经验规律 + 联赛/赛事特性缺口

用户经验: 对阵两队"过去两场比赛的总进球之和 / 2" ≈ 本场总进球。
  例: 主队近2场共进4球, 客队近2场共进5球 → (4+5)/2 = 4.5 ≈ 本场总进球。

本研究在 31.2万场真实赛果上:
  1. 用时间序列方式(每场只用它"之前"的比赛算 form, 杜绝未来 leakage)验证该规律。
  2. 对比朴素基线(全局均值 / 同联赛均值)的 RMSE, 看是否真有增量。
  3. 测试更稳的"攻防分解"变体(Dixon-Coles 式)与"收缩估计"(防 n=2 高方差)。
  4. 单独统计欧冠/资格赛等赛事的总进球均值, 量化"赛事特性"缺口。

铁律: 命中率/误差并排基线; 分箱查单调性; 未知不填0; 不做硬预测。
"""
from __future__ import annotations
import sqlite3
import math
from collections import defaultdict

DB = r"D:\Architecture\data\football_data.db"


def main() -> int:
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT home_team, away_team, match_date, home_score, away_score, league_name
        FROM historical_matches
        WHERE home_score IS NOT NULL AND away_score IS NOT NULL
          AND home_team IS NOT NULL AND away_team IS NOT NULL
          AND match_date IS NOT NULL
    """).fetchall()
    con.close()
    print(f"加载赛果 n={len(rows):,}")

    # ── 按队建立时间序历史 (date 为 ISO 'YYYY-MM-DD', 字符串比较=时间序) ──
    team_hist = defaultdict(list)  # team -> [(date, total, gf, ga), ...]
    for home, away, date, hs, as_, lg in rows:
        tot = hs + as_
        team_hist[home].append((date, tot, hs, as_))        # 主队本场 gf=hs, ga=as_
        team_hist[away].append((date, tot, as_, hs))        # 客队本场 gf=as_, ga=hs
    for t in team_hist:
        team_hist[t].sort(key=lambda x: x[0])

    def prev_form(team, date, k=2):
        ms = [m for m in team_hist[team] if m[0] < date]   # 严格只用"之前"的比赛
        if len(ms) < k:
            return None
        rec = ms[-k:]
        avg_total = sum(m[1] for m in rec) / k
        avg_gf = sum(m[2] for m in rec) / k
        avg_ga = sum(m[3] for m in rec) / k
        return avg_total, avg_gf, avg_ga

    # ── 逐场回测 ──
    ev = []  # (actual, pred_user, pred_attdef, league)
    for home, away, date, hs, as_, lg in rows:
        hf = prev_form(home, date)
        af = prev_form(away, date)
        if hf is None or af is None:
            continue
        actual = hs + as_
        # 用户规律: (主近2总和 + 客近2总和)/2 = 主均total + 客均total
        pred_user = hf[0] + af[0]
        # 攻防分解变体: 主预期=主攻+客防; 客预期=客攻+主防
        # prev_form 返回 (avg_total[0], avg_gf[1], avg_ga[2])
        pred_attdef = (hf[1] + af[2]) + (af[1] + hf[2])
        ev.append((actual, pred_user, pred_attdef, lg))

    n = len(ev)
    print(f"可用(两队均有≥2场前史) n={n:,}  覆盖率={n/len(rows):.1%}\n")

    actuals = [e[0] for e in ev]
    gmean = sum(actuals) / n
    lg_sum, lg_n = defaultdict(float), defaultdict(int)
    for a, _, _, lg in ev:
        lg_sum[lg] += a
        lg_n[lg] += 1
    lg_mean = {l: lg_sum[l] / lg_n[l] for l in lg_n}

    def rmse(pairs):
        return math.sqrt(sum((p - a) ** 2 for a, p in pairs) / len(pairs))

    def corr(xs, ys):
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        dy = math.sqrt(sum((y - my) ** 2 for y in ys))
        return num / (dx * dy) if dx and dy else 0.0

    base_global = [(a, gmean) for a in actuals]
    base_league = [(a, lg_mean[lg]) for a, _, _, lg in ev]
    pred_user = [(a, p) for a, p, _, _ in ev]
    pred_attdef = [(a, q) for a, _, q, _ in ev]

    print("=" * 74)
    print("【总体误差对比】 RMSE 越低越好 (单位: 球)")
    print("=" * 74)
    print(f"  全局均值基线        RMSE={rmse(base_global):.4f}")
    print(f"  同联赛均值基线      RMSE={rmse(base_league):.4f}")
    print(f"  用户规律(近2总和/2) RMSE={rmse(pred_user):.4f}  corr={corr([p for _,p in pred_user],[a for a,_ in pred_user]):.4f}")
    print(f"  攻防分解变体        RMSE={rmse(pred_attdef):.4f}  corr={corr([p for _,p in pred_attdef],[a for a,_ in pred_attdef]):.4f}")

    # ── 收缩估计: pred = w*form + (1-w)*gmean, 扫 w 找最优 ──
    print("\n" + "=" * 74)
    print("【收缩估计】 防 n=2 高方差: pred = w·form + (1-w)·全局均值")
    print("=" * 74)
    best_w, best_r = 0, 1e9
    for w in [0.0, 0.25, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 1.0]:
        pairs = [(a, w * p + (1 - w) * gmean) for a, p in pred_user]
        r = rmse(pairs)
        flag = "  ← 最优" if r < best_r else ""
        if r < best_r:
            best_r, best_w = r, w
        print(f"  w={w:.2f}  RMSE={r:.4f}{flag}")

    # ── 单调性: 分箱看 pred_user 是否随实际单调 ──
    print("\n" + "=" * 74)
    print("【单调性】 用户规律预测值分箱 → 实际均值 (应单调上升)")
    print("=" * 74)
    edges = [0, 1.5, 2.5, 3.5, 4.5, 5.5, 99]
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sub = [a for a, p, _, _ in ev if lo <= p < hi]
        if len(sub) < 200:
            continue
        print(f"  pred∈[{lo:.1f},{hi:.1f})  n={len(sub):6,}  实际均值={sum(sub)/len(sub):.2f}")

    # ── 用户具体场景: pred≈4.5 时实际多少 ──
    sub45 = [a for a, p, _, _ in ev if 4.0 <= p < 5.0]
    if sub45:
        print(f"\n  用户场景 pred∈[4.0,5.0)  n={len(sub45):,}  实际均值={sum(sub45)/len(sub45):.2f}  "
              f"(说明: 规律指向大球时, 实际偏差方向?)")

    # ── 赛事特性缺口: 欧冠/资格赛等总进球均值 ──
    print("\n" + "=" * 74)
    print("【赛事特性】 含'欧冠/冠军/Champions/资格'的联赛名总进球均值")
    print("=" * 74)
    uc = [(l, lg_mean[l], lg_n[l]) for l in lg_mean if any(k in l for k in ["欧冠", "冠军", "Champions", "资格", "CL", "cup", "Cup"])]
    uc.sort(key=lambda x: -x[2])
    print(f"  (全局均值={gmean:.2f})")
    for l, m, c in uc[:15]:
        print(f"  {l[:28]:28} 均值={m:.2f}  n={c:,}")
    print(f"\n  → 五大联赛对照:")
    for l in ["英超", "西甲", "意甲", "德甲", "法甲"]:
        if l in lg_mean:
            print(f"    {l} 均值={lg_mean[l]:.2f}  n={lg_n[l]:,}")

    # ── 结论 ──
    print("\n" + "=" * 74)
    print("【结论口径】")
    print("=" * 74)
    ru, ra, rg = rmse(pred_user), rmse(pred_attdef), rmse(base_league)
    print(f"  用户规律 RMSE={ru:.4f} vs 同联赛基线={rg:.4f} → "
          f"{'真有增量' if ru < rg-0.005 else '增量微弱/无'}")
    print(f"  攻防分解 RMSE={ra:.4f} → {'优于朴素规律' if ra < ru else '不优于'}")
    print(f"  最优收缩 w={best_w:.2f} RMSE={best_r:.4f}")
    print("  注: 规律本身含真实状态动量, 但 n=2 方差大, 必须收缩+并排基线方可入模。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
