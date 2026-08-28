#!/usr/bin/env python3
"""
OU 全场选线策略 A/B 回测 — 锚定线挑偏离 vs 跟随市场主盘
=====================================================
背景 (2026-08-22): select_ou_lines 全场硬编码锚定 {2.0,2.25,2.5}, 但实测
67.4% 比赛主盘偏离锚定线。本回测对比两种"全场 OU 选线"策略对破蛋方向命中率/ROI
的影响, 用于决定是否把锚定线改为"跟随市场主盘(去水最均衡)".

策略:
  - anchor (当前): 在锚定线 {2.0,2.25,2.5} 里挑"去水偏离最大"的线; 锚定线缺失则回退主盘线
  - market (方案): 直接选"去水后最接近 50/50"的线 = 市场主盘

方向判定 (与 probe_core anchor='market' 分支一致): 去水 P(over) > 0.5 → 买大, 否则买小。
结算: ou_settle_fractional (精确处理 split 线半赢/半输/走盘, 与线上修复一致)。
数据: events.db 已结束比赛的最早盘口快照 (开赛 0-0, 全场线全未破)。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.evaluation.ou_eval import ou_settle_fractional

GQ = os.environ.get('GQ_DB', 'D:/Architecture/data/events.db')
ANCHOR = {2.0, 2.25, 2.5}


def earliest_odds(con, match_key):
    """取每场最早(开赛)快照的 over/under 与 1X2, 产出与 probe_core 一致的 odds 字典。"""
    rows = con.execute(
        """SELECT market, selection, odds, captured_at FROM odds_snapshots
           WHERE match_key=? AND selection IN ('over','under','home','draw','away')
             AND odds>1.01 AND odds<1000.0
           ORDER BY captured_at ASC""",
        (match_key,)).fetchall()
    by_mkt = {}
    for mkt, sel, odds, cap in rows:
        by_mkt.setdefault(mkt, {}).setdefault(cap, {})[sel] = odds
    out = {}
    for mkt, caps in by_mkt.items():
        for cap in sorted(caps):
            d = caps[cap]
            if mkt.startswith('OU'):
                if 'over' in d and 'under' in d:
                    out[f'{mkt}__over'] = d['over']
                    out[f'{mkt}__under'] = d['under']
                    break
    return out


def dewatered_over(ov, un):
    po, pu = 1.0 / ov, 1.0 / un
    s = po + pu
    return (po / s) if s > 0 else None


def pick_line(lines, mode):
    """从全场 OU 线里按策略选一条 (line, over, under)。"""
    if not lines:
        return None
    if mode == 'market':
        return min(lines, key=lambda x: abs(dewatered_over(x[1], x[2]) - 0.5))
    anchor = [x for x in lines if x[0] in ANCHOR]
    pool = anchor if anchor else lines
    return max(pool, key=lambda x: abs(dewatered_over(x[1], x[2]) - 0.5))


def main():
    con = sqlite3.connect(GQ)
    matches = con.execute(
        """SELECT match_key, score_home, score_away FROM matches
           WHERE status='finished' AND score_home IS NOT NULL AND score_away IS NOT NULL"""
    ).fetchall()
    print(f"已结束且有赛果的比赛: {len(matches)}")

    results = {}
    for mode in ('anchor', 'market'):
        n = hit = 0
        roi = 0.0
        for mk, sh, sa in matches:
            odds = earliest_odds(con, mk)
            lines = []
            for k in odds:
                if (k.startswith('OU_') and not k.startswith('OU_1H')
                        and not k.startswith('OU_2H') and k.endswith('__over')):
                    lk = k[:-6]
                    ov = odds.get(f'{lk}__over')
                    un = odds.get(f'{lk}__under')
                    if ov and un and ov > 1.01 and un > 1.01:
                        try:
                            line = float(lk.split('_')[-1])
                        except ValueError:
                            continue
                        lines.append((line, ov, un))
            p = pick_line(lines, mode)
            if not p:
                continue
            line, ov, un = p
            p_over = dewatered_over(ov, un)
            if p_over is None:
                continue
            direction_over = p_over > 0.5
            goals = sh + sa
            actual_over = goals > line
            win = direction_over == actual_over
            n += 1
            hit += int(win)
            settle = ou_settle_fractional(goals, line)  # 买大球的注码倍率
            roi += settle if direction_over else -settle
        acc = hit / n * 100 if n else 0.0
        roi_pct = roi / n * 100 if n else 0.0
        results[mode] = {'n': n, 'hit': hit, 'acc': acc, 'roi': roi_pct}
        print(f"  {mode:7s}: 可结算 {n:5d} 场 | 方向命中率 {acc:5.1f}% ({hit}/{n}) | ROI {roi_pct:+5.1f}%")

    print("\n=== 结论 ===")
    a, m = results['anchor'], results['market']
    print(f"  锚定挑偏离 vs 跟随主盘: 命中率 {a['acc']:.1f}% vs {m['acc']:.1f}% "
          f"(Δ{m['acc']-a['acc']:+.1f}pp) | ROI {a['roi']:+.1f}% vs {m['roi']:+.1f}%")


if __name__ == '__main__':
    main()
