#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""滚球断供比分救援 — 一次性回补 (2026-09-08).

背景(用户报"前端有的比赛不显示比分"):
  乐鱼 WS 对部分低级联赛(俄杯资/德国戊级等)一开赛即停止推流 — 快照停在
  开赛瞬间, C103 比分帧从未到达 → matches.score_home 恒 NULL:
    ① 前端滚球列表比分留白;
    ② 模型条件化被迫按 0-0 处理, OU/CS 判定失真。
  实测(2026-09-09 晚): 列表 244 场里 180 场 score=None, 其中真在踢的
  (俄杯资 90' 等)全部断供。

本脚本: structureMatchBaseInfoByMidsPB 端点对滚球场仍返回 msc 真实比分 →
  选【墙钟已开赛 3~100min + score 缺失 + 有 mid + 非 finished + 非 is_override】
  的场次批量拉分回写, 顺带把假 scheduled 状态翻正 live。
  长效机制已进采集器(gq/auto_collector.py GQCollector._rescue_missing_scores,
  daemon 每 90s 一轮); 本脚本用于立即回补当前积压, 或采集器未重启时手动触发。

安全铁律 (同 backfill_scores_api.py):
  - 最小 API client 经 import backfill_scores_api 复用(它不 import
    gq.auto_collector, 避开 msvcrt 单例锁), 本脚本也不 import 采集器。
  - 只补缺失(score_home IS NULL), 不覆盖已有比分; HT 走 COALESCE 不清旧值。
  - apply 前针对性行备份 JSONL; 默认 dry-run。
  - 尊重人工纠偏锁 is_override。

用法:
  python scripts/rescue_live_scores.py            # dry-run
  python scripts/rescue_live_scores.py --apply    # 写库
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backfill_scores_api import fetch_match_structure, _score_from_msc, _safe_print  # noqa: E402

DB = r'D:\Architecture\data\events.db'


def _parse_kickoff(s):
    """matches.kickoff (naive GMT+8 或 ISO+00:00) → Unix 秒。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.timestamp()
    except Exception:
        return None


def main(apply: bool = False):
    con = sqlite3.connect(DB, timeout=30)
    con.execute('PRAGMA busy_timeout=15000')
    con.row_factory = sqlite3.Row
    now = time.time()
    rows = con.execute(
        "SELECT match_key, mid, kickoff, status FROM matches "
        "WHERE mid IS NOT NULL AND mid != '' "
        "AND (score_home IS NULL OR score_away IS NULL) "
        "AND (is_override IS NULL OR is_override=0) "
        "AND status != 'finished'").fetchall()
    targets = []
    for r in rows:
        kots = _parse_kickoff(r['kickoff'])
        if not kots:
            continue
        em = (now - kots) / 60.0
        if 3.0 <= em <= 100.0:
            targets.append((r['match_key'], str(r['mid']), r['status']))
    _safe_print(f"[SELECT] 墙钟在赛(3~100min)+比分缺失: {len(targets)} 场")

    if not targets:
        con.close()
        return
    items = fetch_match_structure([m for _, m, _ in targets])
    by_mid = {}
    for m in items:
        if isinstance(m, dict) and m.get('mid') is not None:
            by_mid[str(m.get('mid'))] = m
    _safe_print(f"[API] structure 返回 {len(items)} 条 (目标 {len(targets)} 场)")

    fixed, skipped = [], 0
    for mk, mid, st in targets:
        m = by_mid.get(mid)
        if not m:
            skipped += 1
            continue
        sh, sa, ht_sh, ht_sa = _score_from_msc(m.get('msc'))
        if sh is None or sa is None:
            skipped += 1
            continue
        # 足球合理性护栏 (2026-09-08): 实测混入篮球场(奥帕瓦=捷克篮球俱乐部,
        # 返回 0-0/HT 5-11) — 半场比分大于全场必然不是足球, 跳过防污染。
        if ht_sh is not None and ht_sa is not None and (ht_sh > sh or ht_sa > sa):
            skipped += 1
            continue
        if sh + sa > 12:
            skipped += 1
            continue
        fixed.append((mk, sh, sa, ht_sh, ht_sa, st))
        _safe_print(f"  {mk} [{st}] → {sh}-{sa}" + (f" (HT {ht_sh}-{ht_sa})" if ht_sh is not None else ""))

    if not apply:
        _safe_print(f"(dry-run) 可回补 {len(fixed)} 场, API 无分跳过 {skipped} 场; 加 --apply 写库")
        con.close()
        return

    if fixed:
        bpath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             f'_rescue_live_backup_{int(time.time())}.jsonl')
        with open(bpath, 'w', encoding='utf-8') as f:
            for mk, sh, sa, ht_sh, ht_sa, st in fixed:
                f.write(json.dumps({'match_key': mk, 'old_status': st,
                                    'new_score': f'{sh}-{sa}'}, ensure_ascii=False) + '\n')
        n = 0
        for mk, sh, sa, ht_sh, ht_sa, st in fixed:
            cur = con.execute(
                "UPDATE matches SET score_home=?, score_away=?, "
                "ht_score_home=COALESCE(?, ht_score_home), "
                "ht_score_away=COALESCE(?, ht_score_away), "
                "status=CASE WHEN status='scheduled' THEN 'live' ELSE status END, "
                "last_seen=? "
                "WHERE match_key=? AND (score_home IS NULL OR score_away IS NULL) "
                "AND (is_override IS NULL OR is_override=0)",
                (sh, sa, ht_sh, ht_sa, time.time(), mk))
            n += cur.rowcount
        con.commit()
        _safe_print(f"[APPLY] 回补 {n} 场, 备份: {bpath}")
    con.close()


if __name__ == '__main__':
    main(apply='--apply' in sys.argv)
