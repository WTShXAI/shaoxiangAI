"""小范围回测: solve_oip λ 修复 (OU 锚定) 在势均力敌子集上的净增益验证。

2026-08-30 修复: predict_score 加 implied_total(OU 锚), 不再用平局概率压 λ。
本脚本在**势均力敌**(去水平局率 pd>0.28)子集上, 对比:
  A 旧: predict_score 不传 implied_total (solve_oip 硬压 λ)
  B 新: predict_score 传 implied_total (从开盘 OU 反推)

指标: 比分 top1/top3 命中率, P(over 2.5) 的 Brier/AUC。
⚠ 干净子集(排除 score_missing/假0-0)。

用法: runpy scripts/backtest_lambda_fix_20260830.py [样本场数]
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")
from scripts.compare_ou_models_20260830 import opening_ou  # noqa: E402
from analysis.live_goal_probe import _open_1x2_from_snapshots  # noqa: E402
from pipeline.score_model import predict_score  # noqa: E402


def dewater(h, d, a):
    ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


def top3_of(r):
    return [f"{a}-{b}" for a, b, p in (r.get("top_scores") or [])[:3]]


def p_over_of(r, line=2.5):
    M = r["matrix"]
    return sum(M[i, j] for i in range(M.shape[0]) for j in range(M.shape[1])
               if i + j > line) / M.sum()


def auc(s, y):
    s = list(s); y = list(y)
    pos = [s[i] for i in range(len(s)) if y[i] == 1]
    neg = [s[i] for i in range(len(s)) if y[i] == 0]
    if not pos or not neg:
        return float('nan')
    t = 0.0
    for p in pos:
        t += sum(1 for q in neg if p > q) + 0.5 * sum(1 for q in neg if p == q)
    return t / (len(pos) * len(neg))


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-15' "
        "ORDER BY kickoff DESC LIMIT ?", (n,)).fetchall()

    stat = {'A旧solve_oip': [0, 0, 0, 0, 0.0, []], 'B新OU锚定': [0, 0, 0, 0, 0.0, []]}
    n_even = 0
    for mk, home, away, sh, sa, ko, league in rows:
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            continue
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            continue
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            continue
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            continue
        ph, pd_, pa = dewater(oh, od, oa)
        # 全量回测(2026-08-30): 不再只取势均力敌, 覆盖所有场景确认无全局退化。
        # 如需势均力敌子集, 加环境变量 EVEN_ONLY=1。
        if os.environ.get("EVEN_ONLY") == "1" and pd_ <= 0.28:
            continue
        ou = opening_ou(con, mk)
        if not ou:
            continue
        line, ov, un = ou
        try:
            po = (1.0 / ov) / (1.0 / ov + 1.0 / un)
            implied_total = line + 2.0 * (po - 0.5)
        except Exception:
            continue
        if not (1.0 < implied_total < 6.0):
            continue
        n_even += 1

        true_s = f"{int(sh)}-{int(sa)}"
        y_over = 1 if (int(sh) + int(sa)) > 2.5 else 0

        rA = predict_score(home, away, oh, od, oa, goal_scale=1.2)
        rB = predict_score(home, away, oh, od, oa, goal_scale=1.2, implied_total=implied_total)

        for tag, r in (("A旧solve_oip", rA), ("B新OU锚定", rB)):
            s = stat[tag]
            s[0] += 1
            t3 = top3_of(r)
            if t3 and t3[0] == true_s:
                s[1] += 1
            if true_s in t3:
                s[2] += 1
            po = p_over_of(r)
            s[4] += (po - y_over) ** 2
            s[5].append((po, y_over))

    print(f"势均力敌(pd>0.28) + 有开盘OU 的干净样本: {n_even}")
    print(f"\n{'方案':<16}{'样本':>7}{'top1':>9}{'top3':>9}{'OU_Brier':>11}{'OU_AUC':>10}")
    print("-" * 62)
    for tag in ("A旧solve_oip", "B新OU锚定"):
        s = stat[tag]
        nn = s[0]
        if not nn:
            continue
        br = s[4] / nn
        a = auc([x[0] for x in s[5]], [x[1] for x in s[5]])
        print(f"{tag:<16}{nn:>7d}{s[1]/nn*100:>8.1f}%{s[2]/nn*100:>8.1f}%{br:>11.4f}{a:>10.4f}")
    a, b = stat['A旧solve_oip'], stat['B新OU锚定']
    if a[0] and b[0]:
        print("-" * 62)
        print(f"{'差(B-A)':<16}{'':>7}{(b[1]/b[0]-a[1]/a[0])*100:>+8.1f}pp"
              f"{(b[2]/b[0]-a[2]/a[0])*100:>+8.1f}pp"
              f"{b[4]/b[0]-a[4]/a[0]:>+11.4f}"
              f"{auc([x[0] for x in b[5]],[x[1] for x in b[5]])-auc([x[0] for x in a[5]],[x[1] for x in a[5]]):>+10.4f}")
    con.close()


if __name__ == "__main__":
    main()
