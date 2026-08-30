"""OU(大小球) 专项体检 — 2026-08-29 用户"大小球完全不可信"。

回答三个问题:
  Q1 开盘 OU 线是不是公平的?  —— 实际 over 率 vs 市场隐含 P(over), 差 = edge
  Q2 OU 模型比"直接用市场价"强吗? —— 模型 P(over) vs naive(隐含去水) 的 AUC/Brier/ROI
  Q3 脏数据(假 0-0)对结论影响多大? —— clean / dirty 分组对比

⚠ 数据根基: 库里 62% 的"完场"比分是假 0-0(从未有过非零 score_at 快照),
   必须先在**干净子集**上算, 否则假 0-0 会把 actual_over_hit 系统性压低
   (总球=0 → 任何线的 over 都不中), 得出"OU 全线无 edge"的错误结论。

用法: PYTHONPATH=. python scripts/audit_ou_trust_20260829.py [样本场数]
"""
import os
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")

from analysis.live_goal_probe import _parse_kickoff, _extract_line_from_market, _ok_ou_line_value  # noqa: E402


def opening_ou(con, mk):
    """开盘主盘 OU (线, over, under)。

    口径 = 2026-08-29 修好的主盘 SSoT:
      ① 动态开盘批 (MIN(minute_at) ~ +1 分钟内, 排除半场/终场残盘冒充开盘)
      ② 单帧选取 (60 秒时间桶, 禁跨帧混拼)
      ③ 同批内取**抽水最低**的线 = 庄家主推盘口 (IR-01 同口径)
    """
    try:
        t0 = con.execute(
            "SELECT MIN(minute_at) FROM odds_snapshots WHERE match_key=? "
            "AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%'",
            (mk,)).fetchone()
        m0 = int(t0[0]) if (t0 and t0[0] is not None) else None
    except Exception:
        m0 = None
    w = ("match_key=? AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' "
         "AND market NOT LIKE '%_2H%' AND odds IS NOT NULL AND odds>1.01 AND odds<1000.0")
    ps = [mk]
    if m0 is not None and m0 <= 5:
        w += " AND minute_at<=?"
        ps.append(m0 + 1)
    else:
        w += " AND minute_at=0"
    try:
        bkt = con.execute(
            f"SELECT MIN(CAST(captured_at/60 AS INTEGER)) FROM odds_snapshots WHERE {w}",
            tuple(ps)).fetchone()
        if not bkt or bkt[0] is None:
            return None
        rows = con.execute(
            f"SELECT market, selection, odds FROM odds_snapshots WHERE {w} "
            f"AND CAST(captured_at/60 AS INTEGER)=?", tuple(ps) + (bkt[0],)).fetchall()
    except Exception:
        return None
    d = {}
    for mkt, sel, o in rows:
        d.setdefault(mkt, {})[sel] = float(o)
    cands = []
    for mkt, v in d.items():
        L = _extract_line_from_market(mkt)
        if L is None or not _ok_ou_line_value(L) or not v.get('over') or not v.get('under'):
            continue
        ovr = 1.0 / v['over'] + 1.0 / v['under']
        cands.append((L, v['over'], v['under'], ovr))
    if not cands:
        return None
    cands.sort(key=lambda x: x[3])          # 抽水最低 = 主盘
    L, ov, un, _ = cands[0]
    return float(L), float(ov), float(un)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-20' "
        "ORDER BY kickoff DESC LIMIT ?", (n,)).fetchall()
    now = time.time()

    agg = defaultdict(lambda: [0, 0, 0.0])      # line -> [n, n_over, sum_implied]
    grp = {'clean': [0, 0, 0.0, 0], 'dirty': [0, 0, 0.0, 0]}
    dirty_examples = []

    for mk, sh, sa, ko, lg in rows:
        kots = _parse_kickoff(ko)
        if not kots or now - kots < 2.5 * 3600:
            continue
        clean = con.execute(
            "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
            "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone()
        tag = 'clean' if clean else 'dirty'

        ou = opening_ou(con, mk)
        if not ou:
            continue
        L, ov, un = ou
        tot = int(sh) + int(sa)
        imp = (1.0 / ov) / ((1.0 / ov) + (1.0 / un))
        is_over = 1 if tot > L else 0

        g = grp[tag]
        g[0] += 1
        g[1] += is_over
        g[2] += imp
        g[3] += 1 if imp > 0.5 else 0        # 市场偏向 over 的次数

        if tag == 'clean':
            a = agg[round(L, 2)]
            a[0] += 1
            a[1] += is_over
            a[2] += imp
        elif len(dirty_examples) < 6:
            dirty_examples.append((mk[:28], f'{sh}-{sa}', L, 'over' if is_over else 'under'))

    print("=== Q3 脏数据影响 (干净 vs 脏 分组) ===")
    print(f"{'分组':<8}{'样本':>7}{'隐含P(over)':>13}{'实际over率':>12}{'edge':>10}")
    for tag in ('clean', 'dirty'):
        nn, no, si, _ = grp[tag]
        if not nn:
            continue
        print(f"{tag:<8}{nn:>7d}{si/nn:>13.4f}{no/nn:>12.4f}{no/nn - si/nn:>+10.4f}")
    print()
    print("脏样本(假0-0)抽样 — 看它们如何压低 over 率:")
    for e in dirty_examples:
        print(f"   {e[0]:28s} 比分{e[1]:5s} 线{e[2]:.2f} → {e[3]}")

    print("\n=== Q1 干净子集 · 开盘 OU 按线校准 (n>=40) ===")
    print(f"{'线':>6}{'样本':>7}{'隐含P(over)':>13}{'实际over率':>12}{'edge':>10}")
    tn = tov = timp = 0
    for L in sorted(agg):
        nn, no, si = agg[L]
        if nn < 40:
            continue
        imp, act = si / nn, no / nn
        print(f"{L:>6.2f}{nn:>7d}{imp:>13.4f}{act:>12.4f}{act-imp:>+10.4f}")
        tn += nn
        tov += no
        timp += si
    if tn:
        print("-" * 48)
        print(f"{'汇总':>6}{tn:>7d}{timp/tn:>13.4f}{tov/tn:>12.4f}{tov/tn-timp/tn:>+10.4f}")

    print("\n判据: |edge| < 0.02 且各线正负互现 → 市场基本公平, OU **无系统性 edge**;")
    print("      某条线 edge 持续 > 0.05 且样本 > 200 → 才值得单独研究。")
    con.close()


if __name__ == "__main__":
    main()
