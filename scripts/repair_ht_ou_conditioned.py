#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""历史数据修复: halftime_conclusion 的 OU 概率条件化 (IR-33 前哨, 2026-09-19).
================================================================================
bug: HT 冻结时若 HT 总球 > OU 盘口, OVER 已数学确定, 但旧读数写入的是
     未条件化的赛前式概率 (486 场平均 0.556, 应为 1.0)。
修复: ht_home+ht_away > ou_line 的行 → ou_prob=1.0, ou_direction='OVER'。
遵循 IR-14: dry-run 先行, --apply 落库, 打印审计清单。

用法: python scripts/repair_ht_ou_conditioned.py [--apply]
"""
import sqlite3
import sys

sys.path.insert(0, r'D:\Architecture')
DB = r'D:\Architecture\data\events.db'


def main():
    apply = '--apply' in sys.argv
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT match_key, ht_home, ht_away, ou_line, ou_direction, ou_prob
        FROM halftime_conclusion
        WHERE ou_line IS NOT NULL AND ht_home IS NOT NULL AND ht_away IS NOT NULL
          AND (ht_home + ht_away) > ou_line""").fetchall()
    bad = [r for r in rows if r['ou_direction'] != 'OVER' or r['ou_prob'] is None or r['ou_prob'] < 0.999]
    print(f'HT总球>盘口(数学确定OVER)的冻结行: {len(rows)}, 其中读数未反映: {len(bad)}')
    for r in bad[:10]:
        print(f"  例: {r['match_key'][:30]} HT {r['ht_home']}-{r['ht_away']} 线{r['ou_line']} "
              f"→ 现值 {r['ou_direction']}/{r['ou_prob']}")
    if not apply:
        print(f'(干跑: 需修复 {len(bad)} 行。加 --apply 执行)')
        con.close()
        return
    with con:
        for r in bad:
            con.execute("""UPDATE halftime_conclusion
                SET ou_prob=1.0, ou_direction='OVER' WHERE match_key=?""", (r['match_key'],))
    print(f'已修复 {len(bad)} 行 (OVER确定态 → prob=1.0)')
    con.close()


if __name__ == '__main__':
    main()
