#!/usr/bin/env python3
"""
OU 破蛋命中率可提升性分析 — 置信度/偏离强度分桶
================================================
问题: 锚定挑偏离 56.1% 命中率, 还能不能调高?
方法: 对每场(锚定挑偏离选线 + 去水方向), 按 ou_confidence 与去水偏离强度(pgap)
     分桶, 看是否存在"高命中率子集" —— 若高置信度桶命中率显著高, 则可加"置信度
     闸门"(低置信弃权)提升整体命中率(代价是减少下注样本量).
"""
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_ou_line_selection import earliest_odds, dewatered_over, pick_line
from pipeline.evaluation.ou_eval import ou_confidence

GQ = os.environ.get('GQ_DB', 'D:/Architecture/data/events.db')


def main():
    con = sqlite3.connect(GQ)
    matches = con.execute(
        """SELECT match_key, score_home, score_away FROM matches
           WHERE status='finished' AND score_home IS NOT NULL AND score_away IS NOT NULL"""
    ).fetchall()

    conf_bucket = defaultdict(lambda: [0, 0])   # confidence -> [hit, n]
    pgap_bucket = defaultdict(lambda: [0, 0])   # pgap -> [hit, n]

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
        p = pick_line(lines, 'anchor')
        if not p:
            continue
        line, ov, un = p
        p_over = dewatered_over(ov, un)
        if p_over is None:
            continue
        direction_over = p_over > 0.5
        goals = sh + sa
        win = direction_over == (goals > line)

        conf = ou_confidence(line, ov, un)
        pgap = abs(p_over - 0.5)

        cb = round(conf * 10) / 10
        conf_bucket[cb][0] += int(win)
        conf_bucket[cb][1] += 1
        pb = round(pgap * 20) / 20   # 0.05 步进
        pgap_bucket[pb][0] += int(win)
        pgap_bucket[pb][1] += 1

    print("=== 按置信度 ou_confidence 分桶 (命中率 vs 样本) ===")
    for b in sorted(conf_bucket):
        h, n = conf_bucket[b]
        print(f"  conf {b:.1f}: {h/n*100:5.1f}%  ({h}/{n})")

    print("\n=== 按去水偏离强度 |P(over)-0.5| 分桶 ===")
    for b in sorted(pgap_bucket):
        h, n = pgap_bucket[b]
        print(f"  pgap {b:.2f}: {h/n*100:5.1f}%  ({h}/{n})")

    # 置信度闸门: 只看 conf >= 阈值的命中率
    print("\n=== 置信度闸门效果(仅高置信下注) ===")
    total_hit = sum(v[0] for v in conf_bucket.values())
    total_n = sum(v[1] for v in conf_bucket.values())
    print(f"  全部: {total_hit/total_n*100:.1f}% ({total_hit}/{total_n})")
    for thr in (0.3, 0.4, 0.5, 0.6, 0.7):
        h = sum(v[0] for b, v in conf_bucket.items() if b >= thr)
        n = sum(v[1] for b, v in conf_bucket.items() if b >= thr)
        if n:
            print(f"  conf>={thr}: {h/n*100:5.1f}% ({h}/{n})  覆盖 {n/total_n*100:.0f}%")


if __name__ == '__main__':
    main()
