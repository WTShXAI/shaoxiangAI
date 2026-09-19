#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电子域深度学习语料构建器 (2026-09-20)
================================================================================
从 data/efootball.db 原始快照构建训练就绪数据集, 落 events.db 独立表 ef_dataset
(隔离: 独立表, 零接触生产预测表; 写入口径幂等可重跑)。

样本粒度 = (比赛, 快照时刻): 引擎状态特征 + 该时刻市场去水概率 → 终局标签。
  特征: minute(引擎分钟), score_diff, total_goals, ph/pd/pa (全场独赢去水),
        odds_moves(此前1X2变动次数)
  标签: label ∈ {0:home,1:draw,2:away} (终局)
基线: 同集市场 LL (即模拟域早盘 1.0121 一线) — 模型必须打败它才算"攻略存在"。

用法: python scripts/efootball_build_dataset.py [--limit-matches N]
"""
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, r'D:\Architecture')
from pipeline.odds_math import devig3

EF_DB = r'D:\Architecture\data\efootball.db'
EV_DB = r'D:\Architecture\data\events.db'

DDL = """
CREATE TABLE IF NOT EXISTS ef_dataset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mid TEXT, ts REAL, minute INTEGER,
    score_h INTEGER, score_a INTEGER,
    ph REAL, pd REAL, pa REAL,
    odds_moves INTEGER,
    label INTEGER,
    UNIQUE(mid, ts));
"""


def parse_1x2(payload: str):
    try:
        d = json.loads(payload)
    except Exception:
        return None
    for grp in d.get('playData') or []:
        if grp.get('hpn') != '全场独赢':
            continue
        pick = {}
        for hl in grp.get('hl') or []:
            for ol in hl.get('ol') or []:
                ot = str(ol.get('ot', '')).strip()
                try:
                    dec = float(ol.get('obv')) / 100000.0
                except (TypeError, ValueError):
                    continue
                if ot == '1' and dec > 1.001:
                    pick['h'] = dec
                elif ot == 'X' and dec > 1.001:
                    pick['d'] = dec
                elif ot == '2' and dec > 1.001:
                    pick['a'] = dec
        if all(k in pick for k in ('h', 'd', 'a')):
            return pick
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit-matches', type=int, default=0)
    args = ap.parse_args()

    ef = sqlite3.connect(f'file:{EF_DB}?mode=ro', uri=True)
    ef.row_factory = sqlite3.Row
    settled = ef.execute("""SELECT mid, final_score FROM ef_matches
        WHERE final_score IS NOT NULL AND final_score<>''""").fetchall()
    if args.limit_matches:
        settled = settled[:args.limit_matches]
    finals = {}
    for r in settled:
        try:
            h, a = r['final_score'].split('-')
            fh, fa = int(h), int(a)
            finals[r['mid']] = 0 if fh > fa else (1 if fh == fa else 2)
        except Exception:
            continue
    snaps = ef.execute("""SELECT mid, payload, captured_at FROM ef_odds_raw
        ORDER BY mid, captured_at""").fetchall()
    ef.close()

    rows = []
    seen_by_mid = {}
    for s in snaps:
        if s['mid'] not in finals:
            continue
        p = parse_1x2(s['payload'])
        if not p:
            continue
        dv = devig3(p['h'], p['d'], p['a'])
        if not dv:
            continue
        seen = seen_by_mid.setdefault(s['mid'], set())
        key = tuple(round(x, 6) for x in sorted(dv) + [p['h'], p['d'], p['a']])
        if key in seen:
            continue  # 赔率未变 → 不重复记样本 (odds_moves 单独计)
        seen.add(key)
        prev = seen_by_mid.get(s['mid'] + '_n', 0)
        rows.append((s['mid'], s['captured_at'], None, None, None,
                     round(dv[0], 6), round(dv[1], 6), round(dv[2], 6), prev, finals[s['mid']]))
        seen_by_mid[s['mid'] + '_n'] = prev + 1

    # 回填比分/分钟: 从原始 payload 里补 (轻量二次解析)
    ef = sqlite3.connect(f'file:{EF_DB}?mode=ro', uri=True)
    ef.row_factory = sqlite3.Row
    by_mid_ts = {}
    for s in ef.execute("SELECT mid, payload, captured_at FROM ef_odds_raw ORDER BY captured_at"):
        by_mid_ts[(s['mid'], s['captured_at'])] = s['payload']
    filled = []
    for mid, ts, _mn, _sh, _sa, ph, pd_, pa, moves, label in rows:
        payload = by_mid_ts.get((mid, ts))
        minute = sh = sa = None
        if payload:
            try:
                d = json.loads(payload)
                md = d.get('data') or []
                md0 = md[0] if isinstance(md, list) and md else {}
                for it in (md0.get('msc') or []):
                    s = str(it)
                    if s.startswith('S0|'):
                        a, b = s[3:].split(':')
                        sh, sa = int(a), int(b)
                        break
                try:
                    minute = int(int(md0.get('mst', 0)) // 60)
                except Exception:
                    minute = None
            except Exception:
                pass
        filled.append((mid, ts, minute, sh, sa, ph, pd_, pa, moves, label))
    ef.close()

    ev = sqlite3.connect(EV_DB, timeout=30)
    ev.executescript(DDL)
    ev.executemany("""INSERT OR IGNORE INTO ef_dataset
        (mid, ts, minute, score_h, score_a, ph, pd, pa, odds_moves, label)
        VALUES(?,?,?,?,?,?,?,?,?,?)""", filled)
    ev.commit()
    n = ev.execute('SELECT COUNT(*) FROM ef_dataset').fetchone()[0]
    n_m = ev.execute('SELECT COUNT(DISTINCT mid) FROM ef_dataset').fetchone()[0]
    # 同集市场基线
    bl = ev.execute("""SELECT COUNT(*), AVG(-CASE label WHEN 0 THEN ln(ph) WHEN 1 THEN ln(pd) ELSE ln(pa) END)
        FROM ef_dataset""").fetchone()
    ev.close()
    print(f'ef_dataset: {len(filled)} 新样本 (表内 {n}), 覆盖 {n_m} 场')
    if bl and bl[0]:
        print(f'同集市场基线: n={bl[0]}, LL={bl[1]:.4f} (模型必须打败此值)')


if __name__ == '__main__':
    main()
