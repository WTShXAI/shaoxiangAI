#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ht_score 历史污染修复 (2026-09-18, 根因: 完场后 feed S1 镜像终场 + anti-clobber 保全).

干净源: odds_changes.score_at 末次上半场快照 (minute_at 40-47, 实测 ht==ft 16.9%/违反0)。
守卫: ① 仅修 ht(=终场 或 物理违反) 的行, 合法 0-0→0-0 闷平不动; ② score_at 缺失不动;
③ 分批短事务 + 撞锁重试 (ws_collector 常驻)。
用法: python scripts/repair_ht_from_score_at.py [--limit 20000] [--dry]
"""
import argparse
import re
import sys
import time

sys.path.insert(0, r'D:\Architecture')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=20000)
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args()
    from analysis.live_goal_probe import _open_gq
    con = _open_gq()
    rows = con.execute("""
        SELECT m.match_key, m.ht_score_home, m.ht_score_away, m.score_home, m.score_away,
               c.score_at
        FROM matches m JOIN odds_changes c ON c.match_key = m.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.ht_score_home IS NOT NULL
        AND c.market='1X2' AND c.minute_at BETWEEN 40 AND 47 AND c.score_at IS NOT NULL
        AND c.minute_at = (SELECT MAX(c2.minute_at) FROM odds_changes c2
            WHERE c2.match_key=m.match_key AND c2.market='1X2'
            AND c2.minute_at BETWEEN 40 AND 47 AND c2.score_at IS NOT NULL)
        LIMIT ?""", (args.limit,)).fetchall()
    n_fix = n_keep = n_skip = 0
    for mk, mh, ma, fsh, fsa, sa in rows:
        try:
            a, b = map(int, re.match(r'(\d+)-(\d+)', sa).groups())
        except Exception:
            n_skip += 1
            continue
        if a > fsh or b > fsa:      # 源本身物理违反 → 不用
            n_skip += 1
            continue
        polluted = (mh > fsh or ma > fsa) or (mh == fsh and ma == fsa and not (a == mh and b == ma))
        # 上面第二支: ht==ft 但干净源不同 → 该 ht 是镜像终场的产物 (合法闷平会被源证实)
        if not polluted:
            n_keep += 1
            continue
        if args.dry:
            n_fix += 1
            continue
        for attempt in range(5):
            try:
                cur = con.cursor()
                cur.execute("UPDATE matches SET ht_score_home=?, ht_score_away=? WHERE match_key=?",
                            (a, b, mk))
                con.commit()
                n_fix += 1
                break
            except Exception:
                if attempt == 4:
                    n_skip += 1
                else:
                    time.sleep(3)
    print(f'扫描 {len(rows)}: 修复 {n_fix} / 合规保留 {n_keep} / 跳过 {n_skip}'
          f'{" (dry)" if args.dry else ""}')
    # 修复后复测
    if not args.dry and n_fix:
        r = con.execute("""SELECT COUNT(*), SUM(CASE WHEN ht_score_home=score_home
            AND ht_score_away=score_away THEN 1 ELSE 0 END),
            SUM(CASE WHEN ht_score_home>score_home OR ht_score_away>score_away THEN 1 ELSE 0 END)
            FROM matches WHERE status='finished' AND score_home IS NOT NULL AND ht_score_home IS NOT NULL""").fetchone()
        print(f'修复后: ht==ft {r[1]} ({r[1]/max(r[0],1)*100:.1f}%), 物理违反 {r[2]}')


if __name__ == '__main__':
    main()
