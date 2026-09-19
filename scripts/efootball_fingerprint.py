#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电子域引擎指纹 — 小规模首跑 (2026-09-19, 用户指令: 23:30 根据采集数据小规模做一次)
================================================================================
输入: data/efootball.db (efootball_probe 采集的 VS- 模拟联赛语料)
输出: reports/efootball_fingerprint.json + 控制台对比表

三件套 (模拟域 vs 真实域同窗对照):
  1. 赛果分布: 主胜/平局/0-0/场均进球 (真实域用 credible_1x2 守卫口径)
  2. 比分集中度: top 比分占比 (引擎生成的分布比真实比赛"整齐")
  3. 市场效率首切: 最早快照的 全场独赢(1X2) 去水 LL vs 终局 — 与真实域
     开赛后 0-5 分钟首个 live tick 的 1X2 LL 同口径对照

口径注意: 模拟域"最早快照"多为已开赛早期 (探针只抓 livedata), 真实域对照同取
开赛后首个 live tick, 两边同为"极早期 in-play"口径, 公平可比。
"""
import json
import math
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, r'D:\Architecture')
from pipeline.odds_math import devig3
from pipeline.settle import result_1x2, credible_1x2
from pipeline.odds_candles import parse_kickoff_ts

EF_DB = r'D:\Architecture\data\efootball.db'
EV_DB = r'D:\Architecture\data\events.db'
OUT = r'D:\Architecture\reports\efootball_fingerprint.json'


def parse_1x2(payload: str):
    """playData → 最早快照的 1X2 十进制赔率 (h,d,a)。obv=赔率×1e5。"""
    try:
        d = json.loads(payload)
    except Exception:
        return None
    for grp in d.get('playData') or []:
        if '独赢' not in str(grp.get('hpn', '')):
            continue
        for hl in grp.get('hl') or []:
            pick = {}
            for ol in hl.get('ol') or []:
                ot = str(ol.get('ot', '')).strip()
                ott = str(ol.get('ott', ''))
                try:
                    dec = float(ol.get('obv')) / 100000.0   # obv 为字符串, 十进制赔率×1e5
                except (TypeError, ValueError):
                    continue
                if dec <= 1.001 or dec > 500:
                    continue
                if ot in ('1', 'home', 'w1') or ott in ('主', '主胜'):
                    pick['home'] = dec
                elif ot in ('X', 'x', 'draw') or ott in ('平', '平局'):
                    pick['draw'] = dec
                elif ot in ('2', 'away', 'w2') or ott in ('客', '客胜'):
                    pick['away'] = dec
            if len(pick) == 3:
                return pick['home'], pick['draw'], pick['away']
    return None


def outcome_stats(scores):
    n = len(scores)
    if not n:
        return None
    hw = sum(1 for h, a in scores if h > a)
    dr = sum(1 for h, a in scores if h == a)
    z0 = sum(1 for h, a in scores if (h, a) == (0, 0))
    tg = sum(h + a for h, a in scores) / n
    top = Counter(scores).most_common(5)
    return {'n': n, 'home_win': round(hw / n, 4), 'draw': round(dr / n, 4),
            'zero_zero': round(z0 / n, 4), 'avg_goals': round(tg, 2),
            'top_scores': [f'{h}-{a} x{c}' for (h, a), c in top]}


def ll_vs(probs, actual):
    eps = 1e-15
    return -math.log(max(probs[actual], eps))


def main():
    rep = {'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
           'scale': 'small-first-run', 'note': '模拟域=VS-EAFC; 真实域=同窗守卫口径'}

    # ── 模拟域 ──
    ef = sqlite3.connect(f'file:{EF_DB}?mode=ro', uri=True)
    settled = ef.execute(
        "SELECT mid, home, away, final_score FROM ef_matches WHERE final_score IS NOT NULL").fetchall()
    scores = []
    for mid, h, a, fs in settled:
        try:
            ph, pa = fs.split('-')
            scores.append((int(ph), int(pa)))
        except Exception:
            continue
    rep['sim_outcomes'] = outcome_stats(scores)

    # 市场效率: 每场最早快照 1X2 vs 终局
    lls, hits, n_used = [], 0, 0
    for mid, h, a, fs in settled:
        row = ef.execute("SELECT payload FROM ef_odds_raw WHERE mid=? ORDER BY captured_at ASC LIMIT 1",
                         (mid,)).fetchone()
        if not row:
            continue
        p = parse_1x2(row[0])
        if not p:
            continue
        act = result_1x2(*fs.split('-'))
        if not act:
            continue
        probs = dict(zip(('home', 'draw', 'away'), devig3(*p) or (0, 0, 0)))
        if sum(probs.values()) <= 0:
            continue
        lls.append(ll_vs(probs, act))
        hits += int(max(probs, key=probs.get) == act)
        n_used += 1
    ef.close()
    if lls:
        rep['sim_market'] = {'n': n_used, 'log_loss': round(sum(lls) / len(lls), 4),
                             'top1': round(hits / len(lls), 4)}
        rep['sim_market']['note'] = '最早快照(多为开赛初期) 1X2 去水 LL vs 终局'

    # ── 真实域同窗对照 ──
    ev = sqlite3.connect(f'file:{EV_DB}?mode=ro', uri=True)
    ev.row_factory = sqlite3.Row
    rows = ev.execute("""SELECT m.match_key, m.score_home, m.score_away, m.kickoff,
        (SELECT MAX(captured_at) FROM odds_changes oc WHERE oc.match_key=m.match_key) AS last_odds
        FROM matches m WHERE m.status='finished' AND m.score_home IS NOT NULL
        AND m.kickoff >= datetime('now','localtime','-7 day')""").fetchall()
    real_scores, rlls, rhits, rn = [], [], 0, 0
    for r in rows:
        ko = parse_kickoff_ts(r['kickoff'] or '')
        if not ko:
            continue
        if not credible_1x2(r['score_home'], r['score_away'], r['last_odds'], ko):
            continue
        real_scores.append((r['score_home'], r['score_away']))
        # 同口径: 开赛后首个 live 1X2 tick (kickoff 后 0-300s)
        t = ev.execute("""SELECT selection, to_odds FROM odds_changes
            WHERE match_key=? AND market='1X2' AND captured_at BETWEEN ? AND ?
            ORDER BY captured_at LIMIT 6""", (r['match_key'], ko, ko + 300)).fetchall()
        first = {}
        for sel, odds in t:
            first.setdefault(sel, float(odds))
        if all(k in first for k in ('home', 'draw', 'away')):
            act = result_1x2(r['score_home'], r['score_away'])
            probs = dict(zip(('home', 'draw', 'away'), devig3(first['home'], first['draw'], first['away']) or (0, 0, 0)))
            if sum(probs.values()) > 0 and act:
                rlls.append(ll_vs(probs, act))
                rhits += int(max(probs, key=probs.get) == act)
                rn += 1
    ev.close()
    rep['real_outcomes'] = outcome_stats(real_scores)
    if rlls:
        rep['real_market_early_inplay'] = {'n': rn, 'log_loss': round(sum(rlls) / len(rlls), 4),
                                           'top1': round(rhits / len(rlls), 4)}

    # ── 渲染 ──
    print('=== 电子域引擎指纹 (小规模首跑) ===')
    for k in ('sim_outcomes', 'real_outcomes'):
        s = rep.get(k)
        if s:
            print(f"{k}: n={s['n']} 主胜{s['home_win']*100:.0f}% 平{s['draw']*100:.0f}% "
                  f"0-0 {s['zero_zero']*100:.0f}% 场均球{s['avg_goals']} top: {s['top_scores'][:3]}")
    for k in ('sim_market', 'real_market_early_inplay'):
        m = rep.get(k)
        if m:
            print(f"{k}: n={m['n']} LL={m['log_loss']} TOP1={m['top1']}")
    sm, rm = rep.get('sim_market'), rep.get('real_market_early_inplay')
    if sm and rm:
        d = sm['log_loss'] - rm['log_loss']
        print(f"\nΔLL(模拟域早盘 - 真实域早in-play) = {d:+.4f} "
              f"{'→ 模拟域市场显著更弱 (攻略空间信号)' if d > 0.02 else '→ 两域市场效率接近或样本不足'}")

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(f'→ {OUT}')


if __name__ == '__main__':
    main()
