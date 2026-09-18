#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K线集成 vs KNN 赛前判定对照台账 (2026-09-15 对照运行启动).

对已完赛且双判定齐备的场次逐场比对:
  - K线集成 (prematch_candles_verdict.direction, Kronos 移植)
  - KNN 相似度 (prematch_conclusion.verdict_code, 现任赛前机关)
输出双方准确率 + 一致率 + 一致时准确率 + 分歧台账。

用法: python scripts/settle_candles_vs_knn.py [--days 7] [--limit 50]
"""
import argparse
import json
import sys

sys.path.insert(0, r'D:\Architecture')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--limit', type=int, default=50)
    args = ap.parse_args()

    from gq.db import conn, ensure_prematch_candles_table
    ensure_prematch_candles_table()
    with conn() as c:
        rows = c.execute("""
            SELECT v.match_key, v.kickoff, v.direction, v.probs, v.confidence,
                   p.verdict_code, m.score_home, m.score_away, m.home, m.away
            FROM prematch_candles_verdict v
            JOIN matches m ON m.match_key = v.match_key
            LEFT JOIN prematch_conclusion p ON p.match_key = v.match_key
            WHERE m.status='finished' AND m.score_home IS NOT NULL
            AND v.kickoff >= datetime('now', 'localtime', ?)
            ORDER BY v.kickoff DESC LIMIT ?""", (f'-{args.days} day', 5000)).fetchall()

    if not rows:
        print(f'近{args.days}天暂无双判定完赛场 (对照运行积累中)')
        return

    n = hit_c = hit_k = agree = agree_hit = both = 0
    ledger = []
    for mk, ko, direction, probs, conf, knn_code, fsh, fsa, home, away in rows:
        actual = 'home' if fsh > fsa else ('draw' if fsh == fsa else 'away')
        n += 1
        c_hit = direction == actual
        hit_c += c_hit
        knn_dir = {'H': 'home', 'D': 'draw', 'A': 'away'}.get(knn_code)
        if knn_dir:
            both += 1
            k_hit = knn_dir == actual
            hit_k += k_hit
            if knn_dir == direction:
                agree += 1
                agree_hit += c_hit
        ledger.append((ko, mk, direction, float(conf or 0), knn_dir or '-', actual, c_hit))

    print(f'── K线集成 vs KNN 对照台账 (近{args.days}天, 完赛 {n} 场) ──')
    print(f'  K线集成 TOP1: {hit_c}/{n} = {hit_c*100/max(n,1):.1f}%')
    if both:
        print(f'  KNN 机关 TOP1: {hit_k}/{both} = {hit_k*100/max(both,1):.1f}% (双齐备口径)')
        print(f'  双方一致率: {agree}/{both} = {agree*100/max(both,1):.1f}%, 一致时准确率 {agree_hit*100/max(agree,1):.1f}%')
    print(f'\n最近 {min(args.limit, len(ledger))} 场:')
    for ko, mk, direction, conf, knn_dir, actual, c_hit in ledger[:args.limit]:
        mark = '✓' if c_hit else '✗'
        print(f'  {mark} {ko} {mk[:30]:32} K线={direction:5}({conf:.2f}) KNN={knn_dir:5} 实际={actual}')


if __name__ == '__main__':
    main()
