# -*- coding: utf-8 -*-
"""统一工作台判定链 2000 场回测 (2026-09-09, 用户需求)

对每场完场比赛取 55-65' 滚球时刻快照, 喂与线上一致的判定链:
  方向 = cross_score.derive_score_cross (领先方先验 ⊕ 即时盘, winner 直出)
  OU   = probe_core (与滚球分析页同一核心)
  CS   = derive_score_cross 的 top3 (unified_scoreline 同源)
对照真实赛果, 输出命中率与分层结论。
"""
import os
import sys
import json
import time
import sqlite3
from collections import defaultdict

sys.path.insert(0, r'D:\Architecture')
from analysis.live_goal_probe import probe_core, _dewater_1x2  # noqa: E402
from pipeline.cross_score import derive_score_cross  # noqa: E402

DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\console_backtest_2000.json'

con = sqlite3.connect(DB, timeout=30)
con.execute('PRAGMA busy_timeout=20000')
con.row_factory = sqlite3.Row

t0 = time.time()
rows = con.execute("""
SELECT m.match_key, m.home, m.away, m.league,
       m.ht_score_home, m.ht_score_away, m.score_home, m.score_away
FROM matches m
WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
  AND EXISTS (SELECT 1 FROM odds_snapshots o WHERE o.match_key=m.match_key
              AND o.minute_at BETWEEN 55 AND 65)
ORDER BY m.kickoff DESC LIMIT 2000""").fetchall()
print(f'候选场次: {len(rows)}, 载入 {time.time()-t0:.0f}s', flush=True)


def window_odds(mk):
    """55-65' 窗口 → 规范名 odds dict (流内自洽, 与线上 get_latest_snapshot_odds v2 同逻辑)"""
    snaps = con.execute("""
        SELECT market, selection, odds, captured_at FROM odds_snapshots
        WHERE match_key=? AND minute_at BETWEEN 55 AND 65
          AND odds>1.01 AND odds<1000
          AND (market='1X2' OR market LIKE 'OU_%' OR market LIKE 'AH_%')
          AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'
          AND market NOT LIKE 'AH_1H%' AND market NOT LIKE 'AH_2H%'
          AND market != 'OU'
        ORDER BY captured_at DESC LIMIT 600""", (mk,)).fetchall()
    d = {}
    for mkt, sel, o, ts in snaps:
        if mkt == '1X2':
            k = f'1X2__{sel}'
            if k not in d:
                d[k] = float(o)
            continue
        try:
            line = float(mkt.split('_')[1])
        except Exception:
            continue
        if not (0.5 <= line <= 10.0):
            continue
        k = f'OU_{line:g}__{sel}' if mkt.startswith('OU') else f'AH_{line:g}__{sel}'
        if k not in d:
            d[k] = float(o)
    return d


st = defaultdict(int)
ou_by_line = defaultdict(lambda: [0, 0])
dir_by_state = defaultdict(lambda: [0, 0])
cs_n = cs_t1 = cs_t3 = 0
ou_n = ou_ok = 0
dir_n = dir_ok = 0
skipped = 0

clean_rows = []
for r in rows:
    mk = r['match_key']
    # 2026-08-31 标签自洽闸门: FT 标签必须 == 进球轨迹末值 (淘汰 WS 断供/归档污染场,
    # 此前 48 场抽查 39 场标签与轨迹矛盾 — 脏标签会让回测虚高到不可信)
    traj = con.execute(
        "SELECT score_at FROM odds_snapshots WHERE match_key=? AND score_at != '' "
        "AND minute_at BETWEEN 1 AND 130 ORDER BY minute_at", (mk,)).fetchall()
    if not traj:
        continue
    last = traj[-1][0].replace(':', '-')
    if last != f"{r['score_home']}-{r['score_away']}":
        continue
    clean_rows.append(r)
rows = clean_rows
print(f'自洽过滤后样本: {len(rows)}', flush=True)

for i, r in enumerate(rows):
    mk = r['match_key']
    sh, sa = r['score_home'], r['score_away']
    act_dir = 'H' if sh > sa else ('D' if sh == sa else 'A')
    _hth = r['ht_score_home'] if r['ht_score_home'] is not None else 0
    _hta = r['ht_score_away'] if r['ht_score_away'] is not None else 0
    lead = ('H' if _hth > _hta else ('D' if _hth == _hta else 'A')) if r['ht_score_home'] is not None else None
    odds = window_odds(mk)
    if len(odds) < 4:
        skipped += 1
        continue
    cur = f"{r['ht_score_home'] or 0}-{r['ht_score_away'] or 0}"
    minute = 60

    # ── 方向 (cross_score winner 直出) ──
    try:
        r2 = derive_score_cross(con, mk, cur, minute)
        w = (r2 or {}).get('winner')
        if w:
            w = {'home': 'H', 'draw': 'D', 'away': 'A'}.get(w, w)   # 统一枚举
            dir_n += 1
            dir_ok += (w == act_dir)
            key = ('agree' if w == lead else 'conflict') if lead and lead != 'D' else 'no_lead'
            dir_by_state[key][0] += 1
            dir_by_state[key][1] += (w == act_dir)
    except Exception:
        pass

    # ── OU (probe_core, 线上同核心) ──
    try:
        pr = probe_core(odds, cur, minute, r['league'], con, mk)
        fu = pr.get('full') or {}
        if fu.get('data_source') == 'live_odds' and fu.get('direction') in ('OVER', 'UNDER') and fu.get('line'):
            ln = float(fu['line'])
            if ln > (sh + sa) - 0.01:      # 未破线才有方向意义
                total = sh + sa
                actual_over = total > ln
                pred_over = fu['direction'] == 'OVER'
                ou_n += 1
                ou_ok += (pred_over == actual_over)
                b = ou_by_line[round(ln * 2) / 2]
                b[0] += (pred_over == actual_over)
                b[1] += 1
    except Exception:
        pass

    # ── CS (unified top3) ──
    try:
        r3 = derive_score_cross(con, mk, cur, minute)
        t3 = [str(t.get('score', '')).replace(':', '-') for t in ((r3 or {}).get('top3') or [])]
        if t3:
            cs_n += 1
            cs_t1 += (t3[0] == f'{sh}-{sa}')
            cs_t3 += (f'{sh}-{sa}' in t3)
    except Exception:
        pass

    if (i + 1) % 200 == 0:
        print(f'进度 {i+1}/{len(rows)} | 方向 {dir_ok}/{dir_n} | OU {ou_ok}/{ou_n} | CS {cs_t1}/{cs_n} | {time.time()-t0:.0f}s', flush=True)

print('\n===== 统一工作台判定链回测结论 (55-65\' 滚球时刻) =====', flush=True)
print(f'样本: 场次 {len(rows)}, 跳过(无快照) {skipped}')
if dir_n:
    print(f'方向(胜平负·领先方先验⊕即时盘): {dir_ok}/{dir_n} = {dir_ok/dir_n*100:.1f}%')
    for k, (n, ok) in sorted(dir_by_state.items()):
        if n:
            print(f'  {k:10}: {ok}/{n} = {ok/n*100:.1f}%')
if ou_n:
    print(f'OU 大小方向(动态高线, live_odds): {ou_ok}/{ou_n} = {ou_ok/ou_n*100:.1f}%')
    for ln, (ok, n) in sorted(ou_by_line.items()):
        if n >= 10:
            print(f'  线 {ln}: {ok}/{n} = {ok/n*100:.1f}%')
if cs_n:
    print(f'CS 比分: top1 {cs_t1}/{cs_n} = {cs_t1/cs_n*100:.1f}% | top3 {cs_t3}/{cs_n} = {cs_t3/cs_n*100:.1f}%')
print(f'耗时 {time.time()-t0:.0f}s')

json.dump({'dir': dir_ok, 'dir_n': dir_n, 'ou': ou_ok, 'ou_n': ou_n,
           'cs_t1': cs_t1, 'cs_t3': cs_t3, 'cs_n': cs_n, 'rows': len(rows)},
          open(OUT, 'w', encoding='utf-8'), ensure_ascii=False)
