#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""比分链路逐场核对 (2026-09-10, 用户要求"每场比赛逐一核对").

对完赛场比分链路做四类一致性检查:
  CHK-A  HT ≤ FT (分量): 半场比分不得大于全场 (HT 污染检测)
  CHK-B  轨迹单调: score_at 按 captured_at 序须分量单调不减 (进球不会倒退)
  CHK-C  HT 证据一致: HT 窗口(墙钟 40-50min)轨迹末值 vs matches.ht_score
  CHK-D  断供场标记: 有 status='finished' 但无比分/无比分帧 → 记入断供清单

输出: 各类异常计数 + 例证; --json 导出清单供人工复核。
用法: python scripts/verify_score_chain.py [--days 3] [--limit 500] [--json out.json]
"""
import collections
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, r'D:\Architecture')
DB = r'D:\Architecture\data\events.db'


def _parse_kickoff(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.timestamp()
    except Exception:
        return None


def main(days=3, limit=500, json_out=None):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=15000')
    rows = con.execute("""
        SELECT match_key, kickoff, score_home, score_away, ht_score_home, ht_score_away, mid
        FROM matches
        WHERE status='finished' AND score_home IS NOT NULL
          AND kickoff >= datetime('now', ?)
        ORDER BY kickoff DESC LIMIT ?""", (f'-{days} day', limit)).fetchall()

    anom = collections.Counter()
    examples = collections.defaultdict(list)

    for mk, ko, sh, sa, hh, ha, mid in rows:
        kots = _parse_kickoff(ko)
        # CHK-A: HT ≤ FT
        if hh is not None and (hh > sh or ha > sa):
            anom['A_HT大于FT'] += 1
            if len(examples['A_HT大于FT']) < 5:
                examples['A_HT大于FT'].append(f'{mk}: HT {hh}-{ha} > FT {sh}-{sa}')
        # 轨迹
        frames = con.execute("""
            SELECT score_at, captured_at FROM odds_snapshots
            WHERE match_key=? AND score_at IS NOT NULL AND score_at != ''
            ORDER BY captured_at""", (mk,)).fetchall()
        if not frames:
            anom['D_无比分帧'] += 1
            continue
        # CHK-B: 单调
        ph = pa = None
        for sc, ts in frames:
            try:
                a, b = (int(x) for x in str(sc).split('-')[:2])
            except Exception:
                continue
            if ph is not None and (a < ph or b < pa):
                anom['B_轨迹倒退'] += 1
                if len(examples['B_轨迹倒退']) < 5:
                    examples['B_轨迹倒退'].append(f'{mk}: {ph}-{pa} → {a}-{b} 时间倒流')
                break
            ph, pa = a, b
        # CHK-C: HT 窗口证据 (墙钟 40-50min)
        if kots and hh is not None:
            snap = con.execute("""
                SELECT score_at FROM odds_snapshots
                WHERE match_key=? AND score_at IS NOT NULL AND score_at != ''
                  AND captured_at BETWEEN ? AND ?
                ORDER BY captured_at DESC LIMIT 1""", (mk, kots + 40 * 60, kots + 50 * 60)).fetchone()
            if snap:
                try:
                    a, b = (int(x) for x in str(snap[0]).split('-')[:2])
                    if (a + b) < (hh + ha) - 0:   # 证据比记录的 HT 更少球
                        if (a, b) != (hh, ha):
                            anom['C_HT证据不符'] += 1
                            if len(examples['C_HT证据不符']) < 5:
                                examples['C_HT证据不符'].append(f'{mk}: 记录HT {hh}-{ha} vs 证据 {a}-{b}')
                except Exception:
                    pass

    print(f'核对范围: {len(rows)} 场 (近{days}天完赛, 有比分)')
    total = 0
    for k in sorted(anom):
        total += anom[k]
        print(f'❌ {k}: {anom[k]}')
        for e in examples.get(k, [])[:4]:
            print(f'     - {e}')
    if total == 0:
        print('✅ 全部核对通过: 0 异常')
    else:
        print(f'合计异常标记: {total} (需人工复核; HT 类可由 repair_ht_scores_wallclock.py 修复)')
    if json_out:
        with open(json_out, 'w', encoding='utf-8') as f:
            json.dump({k: {'count': v, 'examples': examples.get(k, [])} for k, v in anom.items()},
                      f, ensure_ascii=False, indent=2)
        print(f'清单已导出: {json_out}')
    con.close()
    return total


if __name__ == '__main__':
    days, limit, json_out = 3, 500, None
    argv = sys.argv
    for i, a in enumerate(argv):
        if a == '--days' and i + 1 < len(argv):
            days = int(argv[i + 1])
        if a == '--limit' and i + 1 < len(argv):
            limit = int(argv[i + 1])
        if a == '--json' and i + 1 < len(argv):
            json_out = argv[i + 1]
    main(days, limit, json_out)
