#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""HT 比分腐坏修复 — kickoff 墙钟口径 (2026-09-08).

背景(实测):
  events.db 11432 场 finished 带 HT 的比赛里 5263 场(46.0%) ht==ft(非零进球),
  抽样 29 场有 HT 窗口轨迹证据的 27 场(93%)被证伪(如 皇马 4-1 记 HT 4-1,
  轨迹 45' 实为 1-1)。腐坏来源: 比分回填/同步把 FT 写进 HT 字段
  (live S1| 帧缺失时)。

修复算法(只动有腐坏签名 ht==ft 的行, 保守):
  1. wall-clock HT 窗口 = captured_at - kickoff ∈ [40, 50] 分钟(中场 ±5)。
     score 随时间单调不减 → 窗口内**最后**一条 score_at 即最接近真实 HT 的读数。
  2. 窗口读数与存储 ht 不一致 → 用窗口读数覆盖(matches 表)。
  3. 无窗口证据的行不动(诚实保留, 不猜)。
  4. 每笔修改先落 JSONL 备份(scripts/_ht_repair_backup_<ts>.jsonl), 可回滚。

用法:
  python scripts/repair_ht_scores_wallclock.py            # dry-run 只报告
  python scripts/repair_ht_scores_wallclock.py --apply    # 实际写库
"""
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, r'D:\Architecture')

DB = r'D:\Architecture\data\events.db'
HT_WIN_LO, HT_WIN_HI = 40 * 60, 50 * 60   # 墙钟 HT 窗口(秒)


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


def _parse_score(s):
    try:
        a, b = str(s).split('-')
        return int(a), int(b)
    except Exception:
        return None


def main(apply=False):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=15000')
    rows = con.execute("""
        SELECT match_key, kickoff, score_home, score_away, ht_score_home, ht_score_away
        FROM matches
        WHERE status='finished' AND kickoff IS NOT NULL
          AND ht_score_home IS NOT NULL
          AND ht_score_home=score_home AND ht_score_away=score_away
          AND (score_home+score_away)>0""").fetchall()
    print(f'腐坏签名候选(ht==ft 非零): {len(rows)} 场')

    repairs, evidence_n, agree_n = [], 0, 0
    for mk, ko, sh, sa, hh, ha in rows:
        kots = _parse_kickoff(ko)
        if not kots:
            continue
        # 墙钟 HT 窗口内最后一条带比分的快照 (score 单调 → 最后=最接近真实 HT)
        snap = con.execute("""
            SELECT score_at, captured_at FROM odds_snapshots
            WHERE match_key=? AND score_at IS NOT NULL AND score_at!=''
              AND captured_at BETWEEN ? AND ?
            ORDER BY captured_at DESC LIMIT 1""", (mk, kots + HT_WIN_LO, kots + HT_WIN_HI)).fetchone()
        if not snap:
            continue
        sc = _parse_score(snap[0])
        if sc is None:
            continue
        evidence_n += 1
        if (sc[0] == hh and sc[1] == ha):
            agree_n += 1          # 轨迹支持 ht==ft, 合法(全部进球在上半场)
            continue
        repairs.append((mk, (hh, ha), sc, ko))

    print(f'有墙钟 HT 窗口证据: {evidence_n} 场 (其中轨迹支持 ht==ft 合法: {agree_n})')
    print(f'需修复(证据与存储 HT 矛盾): {len(repairs)} 场')
    for mk, old, new, ko in repairs[:15]:
        print(f'  {mk} [{ko}] ht {old[0]}-{old[1]} → {new[0]}-{new[1]}')
    if len(repairs) > 15:
        print(f'  ... 共 {len(repairs)} 场')

    if apply and repairs:
        bpath = rf'D:\Architecture\scripts\_ht_repair_backup_{int(time.time())}.jsonl'
        with open(bpath, 'w', encoding='utf-8') as f:
            for mk, old, new, ko in repairs:
                f.write(json.dumps({'match_key': mk, 'kickoff': ko,
                                    'old_ht': list(old), 'new_ht': list(new)}, ensure_ascii=False) + '\n')
        done = 0
        for mk, old, new, ko in repairs:
            con.execute("UPDATE matches SET ht_score_home=?, ht_score_away=? WHERE match_key=?",
                        (new[0], new[1], mk))
            done += 1
        con.commit()
        print(f'✅ 已修复 {done} 场, 备份: {bpath}')
    elif not apply:
        print('(dry-run, 加 --apply 写库)')
    con.close()


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
