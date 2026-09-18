#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gq/match_narrative.py — 比赛叙事特征记录层 (2026-09-19 架构升级 B1)
================================================================================
目的: 把每场比赛的"故事"沉淀成结构化特征, 供日后训练 (平局/反超/进球干旱/热门失分等)。

数据源 (全为已采集事实, 零外呼):
  - odds_snapshots.score_at + minute_at (进球时间轴, 与 bridge 进球轨迹同口径: captured_at 序
    + 0-0 帧跳过 + 比分回退回滚 + 分钟单调钳制)
  - matches (HT/FT 比分, kickoff, league)
  - match_outcomes (op_1x2_*/op_ah_line/op_ou_line 开盘市场, 为 1X2/AH/OU/CS 市场快照)

落库: events.db.match_narrative (每场一行, 幂等 UPSERT; 完赛后写入, 可重复重算)

用法:
  python -m gq.match_narrative --match "主队 vs 客队"     # 单场计算并打印
  python -m gq.match_narrative --backfill-days 45        # 历史回填 (只补空行)
  在 scripts/recheck_analysis.py 每日流程中对新完赛场自动调用。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, r'D:\Architecture')

TABLE = 'match_narrative'

TABLE_DDL = """
CREATE TABLE IF NOT EXISTS match_narrative (
    match_key   TEXT PRIMARY KEY,
    kickoff     TEXT,
    league      TEXT,
    home        TEXT,
    away        TEXT,
    ft_home     INTEGER,
    ft_away     INTEGER,
    ht_home     INTEGER,
    ht_away     INTEGER,
    result      TEXT,
    total_goals INTEGER,
    -- 市场快照 (开盘, 去水概率)
    o1x2_home   REAL,
    o1x2_draw   REAL,
    o1x2_away   REAL,
    fav_side    TEXT,
    fav_prob    REAL,
    ah_line     REAL,
    ou_line     REAL,
    has_cs_odds INTEGER,
    -- 进球时间轴特征
    n_goals      INTEGER,
    n_home       INTEGER,
    n_away       INTEGER,
    h1_goals     INTEGER,
    h2_goals     INTEGER,
    first_goal_min INTEGER,
    last_goal_min  INTEGER,
    goal_minutes_json TEXT,
    -- 平局叙事
    draw_at_ht    INTEGER,
    draw_ft       INTEGER,
    draw_episodes INTEGER,
    equalizer     INTEGER,
    late_equalizer INTEGER,
    -- 反超叙事
    comeback        INTEGER,
    comeback_side   TEXT,
    comeback_deficit INTEGER,
    -- 热门失分
    fav_draw   INTEGER,
    fav_failed  INTEGER,
    -- 进球后干旱 (分钟)
    drought_after_first REAL,
    max_goal_gap        REAL,
    h1_drought_to_ht    REAL,
    h2_drought_to_ft    REAL,
    scoreless_last15    INTEGER,
    -- 时间轴 (原始, 供未来特征重算)
    timeline_json TEXT,
    verified      INTEGER,
    score_source  TEXT,
    computed_at   REAL
)
"""

STATE_KEYS = ('result', 'total_goals', 'draw_ft', 'fav_draw', 'fav_failed', 'comeback',
              'comeback_side', 'equalizer', 'late_equalizer', 'scoreless_last15')


# ── 进球时间轴 (与 bridge 进球轨迹同口径) ─────────────────────────────────

def extract_goal_events(con, match_key: str) -> List[Dict]:
    """odds_snapshots.score_at → 单调进球事件 [{min, h, a}]。
    口径: captured_at 序 / 跳过 0-0 帧 / 比分回退回滚 / 显示分钟单调钳制。"""
    traj = con.execute(
        "SELECT minute_at, score_at FROM odds_snapshots WHERE match_key=? AND score_at != '' "
        "AND minute_at BETWEEN 1 AND 130 ORDER BY captured_at", (match_key,)).fetchall()
    seen: List[Dict] = []
    last = (0, 0)
    last_mn = 0
    for mn, sc in traj:
        try:
            a, b = (int(x) for x in str(sc).split('-')[:2])
        except Exception:
            continue
        if (a, b) == (0, 0) or (a, b) == last:
            continue
        if a < last[0] or b < last[1]:
            while seen:
                t = _parse_ts(seen[-1]['score'])
                if t[0] > a or t[1] > b:
                    seen.pop()
                else:
                    break
            if seen:
                last = _parse_ts(seen[-1]['score'])
                last_mn = seen[-1]['min']
        if (a, b) != last:
            mn_c = max(int(mn), last_mn)  # 分钟单调钳制
            seen.append({'min': mn_c, 'score': f'{a}-{b}', 'h': a, 'b': b})
            last = (a, b)
            last_mn = mn_c
    # 事件 = 相邻状态的差分归属 (谁进了这一球)
    events: List[Dict] = []
    prev = (0, 0)
    for e in seen:
        dh, da = e['h'] - prev[0], e['b'] - prev[1]
        side = 'home' if dh > 0 else ('away' if da > 0 else 'none')
        events.append({'min': e['min'], 'h': e['h'], 'a': e['b'], 'side': side})
        prev = (e['h'], e['b'])
    return events


def _parse_ts(score: str) -> Tuple[int, int]:
    a, b = str(score).split('-')[:2]
    return int(a), int(b)


# ── 叙事计算 ──────────────────────────────────────────────────────────────

def compute_narrative(con, match_key: str) -> Optional[Dict]:
    m = con.execute("""SELECT kickoff, score_home, score_away,
        home, away, league FROM matches WHERE match_key=?""", (match_key,)).fetchone()
    if not m or m[1] is None or m[2] is None:
        return None
    kickoff, fsh, fsa, home, away, league = m
    events = extract_goal_events(con, match_key)

    # 终场分权威源 (2026-09-19 假0-0治理): 时间轴非空 → feed 为准 (含修正 matches 停留 0-0 的场);
    # 时间轴为空且记分牌 0-0 → 采集器从未见过该场, 高度疑似假0-0 → verified=0 供训练过滤。
    verified = 1 if events else 0
    score_source = 'matches'
    if events:
        fsh_t, fsa_t = events[-1]['h'], events[-1]['a']
        last_min = events[-1]['min']
        if (fsh_t, fsa_t) != (fsh, fsa):
            if last_min >= 80:
                # 最后事件≥80' → 采集确实跑到终场附近, 时间轴可信
                score_source = 'timeline'
                fsh, fsa = fsh_t, fsa_t
            else:
                # 时间轴提前断流, 无法裁决 → 保守标记不可训练
                verified = 0
                score_source = 'conflict_unresolved'

    # HT 状态由时间轴推导 (matches.ht_score 列 51.2% 回填污染, 弃用 — 与 HT锚训练同决策)。
    # H1 边界 = 49' (与 HT锚 HT_WIN_MIN=50 冻结窗口一致)。
    h1ev = [e for e in events if e['min'] <= 49]
    if h1ev:
        hsh, hsa = h1ev[-1]['h'], h1ev[-1]['a']
    elif events:
        hsh, hsa = 0, 0          # 有时间轴但上半场无进球
    else:
        hsh = hsa = None         # 无滚球覆盖, HT 未知

    total = fsh + fsa
    result = 'home' if fsh > fsa else ('draw' if fsh == fsa else 'away')

    # 市场快照 (开盘)
    mo = con.execute("""SELECT op_1x2_h, op_1x2_d, op_1x2_a, op_ah_line, op_ou_line, op_cs
        FROM match_outcomes WHERE mid=(SELECT mid FROM matches WHERE match_key=?)""",
        (match_key,)).fetchone()
    o1x2 = [None, None, None]
    fav_side, fav_prob = None, None
    has_cs = 0
    if mo:
        if all(mo[i] and mo[i] > 1 for i in range(3)):
            inv = 1 / mo[0] + 1 / mo[1] + 1 / mo[2]
            o1x2 = [(1 / mo[0]) / inv, (1 / mo[1]) / inv, (1 / mo[2]) / inv]
            hi = max(range(3), key=lambda i: o1x2[i])
            if hi != 1:  # 平局不算"热门方"
                fav_side = ('home', 'none', 'away')[hi]
                fav_prob = round(o1x2[hi], 4)
        has_cs = 1 if mo[5] else 0

    # 进球分段
    mins = [e['min'] for e in events]
    h1 = [e for e in events if e['min'] <= 49]
    h2 = [e for e in events if e['min'] > 49]
    n_home = sum(1 for e in events if e['side'] == 'home')
    n_away = sum(1 for e in events if e['side'] == 'away')

    # 平局叙事: 平局状态回合数 (进球把比分为平的次数), 追平, 追平时点
    draw_episodes = 0
    equalizer = 0
    late_equalizer = 0
    prev_state = (0, 0)
    drawn = False
    for e in events:
        st = (e['h'], e['a'])
        if st[0] == st[1] and st != prev_state:
            draw_episodes += 1
            drawn = True
            # 追平: 落后一方扳平
            if prev_state[0] != prev_state[1]:
                equalizer = 1
                if e['min'] >= 80:
                    late_equalizer = 1
        prev_state = st
    draw_at_ht = 1 if (hsh is not None and hsa is not None and hsh == hsa) else 0
    draw_ft = 1 if result == 'draw' else 0
    # 反超: 曾落后 (任意时点净差为负/正) 最终获胜方即为反超方
    comeback, comeback_side, comeback_deficit = 0, None, None
    lead_max = {}  # side -> 最大领先
    st = (0, 0)
    traj_states = [(0, 0)]
    for e in events:
        st = (e['h'], e['a'])
        traj_states.append(st)
    for (hh, aa) in traj_states:
        if hh - aa > 0:
            lead_max['home'] = max(lead_max.get('home', 0), hh - aa)
        elif aa - hh > 0:
            lead_max['away'] = max(lead_max.get('away', 0), aa - hh)
    if result == 'home' and lead_max.get('away', 0) > 0:
        comeback, comeback_side, comeback_deficit = 1, 'home', lead_max['away']
    elif result == 'away' and lead_max.get('home', 0) > 0:
        comeback, comeback_side, comeback_deficit = 1, 'away', lead_max['home']

    # 热门失分
    fav_draw = 1 if (fav_side and result == 'draw') else 0
    fav_failed = 1 if (fav_side and result != fav_side) else 0

    # 进球后干旱
    def _gap(a, b):
        return round(b - a, 1) if b is not None and a is not None else None

    drought_after_first = None
    max_goal_gap = None
    if len(mins) >= 2:
        gaps = [b - a for a, b in zip(mins, mins[1:])]
        drought_after_first = round(gaps[0], 1)
        max_goal_gap = round(max(gaps), 1)
    elif len(mins) == 1:
        drought_after_first = round(90 - mins[0], 1)
        max_goal_gap = drought_after_first
    last_h1_goal = max([e['min'] for e in h1], default=None)
    h1_drought_to_ht = _gap(last_h1_goal, 49) if h1 else 49.0
    last_goal = mins[-1] if mins else None
    h2_drought_to_ft = _gap(last_goal, 90) if (h2 or (last_goal is not None and last_goal <= 45)) else None
    if last_goal is None:
        h2_drought_to_ft = 45.0
    scoreless_last15 = 1 if (last_goal is None or last_goal <= 75) else 0

    ht_row = (hsh, hsa) if (hsh is not None and hsa is not None) else (None, None)

    return {
        'match_key': match_key, 'kickoff': kickoff, 'league': league,
        'home': home, 'away': away,
        'ft_home': fsh, 'ft_away': fsa,
        'ht_home': ht_row[0], 'ht_away': ht_row[1],
        'result': result, 'total_goals': total,
        'o1x2_home': _r4(o1x2[0]), 'o1x2_draw': _r4(o1x2[1]), 'o1x2_away': _r4(o1x2[2]),
        'fav_side': fav_side, 'fav_prob': fav_prob,
        'ah_line': (mo[3] if mo else None), 'ou_line': (mo[4] if mo else None),
        'has_cs_odds': has_cs,
        'n_goals': len(events), 'n_home': n_home, 'n_away': n_away,
        'h1_goals': len(h1), 'h2_goals': len(h2),
        'first_goal_min': mins[0] if mins else None,
        'last_goal_min': mins[-1] if mins else None,
        'goal_minutes_json': json.dumps(mins),
        'draw_at_ht': draw_at_ht, 'draw_ft': draw_ft,
        'draw_episodes': draw_episodes,
        'equalizer': equalizer, 'late_equalizer': late_equalizer,
        'comeback': comeback, 'comeback_side': comeback_side,
        'comeback_deficit': comeback_deficit,
        'fav_draw': fav_draw, 'fav_failed': fav_failed,
        'drought_after_first': drought_after_first,
        'max_goal_gap': max_goal_gap,
        'h1_drought_to_ht': h1_drought_to_ht,
        'h2_drought_to_ft': h2_drought_to_ft,
        'scoreless_last15': scoreless_last15,
        'timeline_json': json.dumps(events, ensure_ascii=False),
        'verified': verified, 'score_source': score_source,
        'computed_at': time.time(),
    }


def _r4(v):
    return round(v, 4) if v is not None else None


# ── 落库 ──────────────────────────────────────────────────────────────────

def ensure_narrative_table(con) -> None:
    con.executescript(TABLE_DDL)
    for col, typ in (('verified', 'INTEGER'), ('score_source', 'TEXT')):
        try:
            con.execute(f'ALTER TABLE {TABLE} ADD COLUMN {col} {typ}')
        except Exception:
            pass  # 已有列


COLS = ['match_key', 'kickoff', 'league', 'home', 'away', 'ft_home', 'ft_away',
        'ht_home', 'ht_away', 'result', 'total_goals', 'o1x2_home', 'o1x2_draw',
        'o1x2_away', 'fav_side', 'fav_prob', 'ah_line', 'ou_line', 'has_cs_odds',
        'n_goals', 'n_home', 'n_away', 'h1_goals', 'h2_goals', 'first_goal_min',
        'last_goal_min', 'goal_minutes_json', 'draw_at_ht', 'draw_ft', 'draw_episodes',
        'equalizer', 'late_equalizer', 'comeback', 'comeback_side', 'comeback_deficit',
        'fav_draw', 'fav_failed', 'drought_after_first', 'max_goal_gap',
        'h1_drought_to_ht', 'h2_drought_to_ft', 'scoreless_last15',
        'timeline_json', 'verified', 'score_source', 'computed_at']


def record_match_narrative(con, match_key: str, narrative: Optional[Dict] = None) -> Optional[Dict]:
    """计算并 UPSERT 一场叙事 (幂等)。narrative 缺省时现算。"""
    if narrative is None:
        narrative = compute_narrative(con, match_key)
    if narrative is None:
        return None
    vals = [narrative.get(c) for c in COLS]
    ph = ','.join('?' * len(COLS))
    updates = ','.join(f'{c}=excluded.{c}' for c in COLS[1:])
    con.execute(f"""INSERT INTO {TABLE} ({','.join(COLS)}) VALUES ({ph})
        ON CONFLICT(match_key) DO UPDATE SET {updates}""", vals)
    return narrative


def backfill(days: int, only_missing: bool = True) -> int:
    from gq.db import conn
    done = 0
    with conn() as con:
        ensure_narrative_table(con)
        if only_missing:
            rows = con.execute("""SELECT m.match_key FROM matches m
                LEFT JOIN match_narrative n ON n.match_key = m.match_key
                WHERE m.status='finished' AND m.score_home IS NOT NULL AND n.match_key IS NULL
                  AND m.kickoff >= datetime('now', ?) ORDER BY m.kickoff""",
                (f'-{days} day',)).fetchall()
        else:
            rows = con.execute("""SELECT match_key FROM matches
                WHERE status='finished' AND score_home IS NOT NULL
                  AND kickoff >= datetime('now', ?) ORDER BY kickoff""",
                (f'-{days} day',)).fetchall()
        for (mk,) in rows:
            try:
                if record_match_narrative(con, mk) is not None:
                    done += 1
            except Exception as e:
                print(f'  [{mk[:32]}] 失败: {e}')
    return done


def main():
    ap = argparse.ArgumentParser(description='比赛叙事特征记录')
    ap.add_argument('--match')
    ap.add_argument('--backfill-days', type=int, default=0)
    ap.add_argument('--refill', action='store_true', help='重算已有行')
    args = ap.parse_args()

    if args.match:
        from gq.db import conn
        with conn() as con:
            ensure_narrative_table(con)
            n = record_match_narrative(con, args.match)
        print(json.dumps(n, ensure_ascii=False, indent=2) if n else '无比分/无该场')
        return

    if args.backfill_days > 0:
        t0 = time.time()
        done = backfill(args.backfill_days, only_missing=not args.refill)
        print(f'回填 {done} 场 [{time.time()-t0:.0f}s]')


if __name__ == '__main__':
    main()
