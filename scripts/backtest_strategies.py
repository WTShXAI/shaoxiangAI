#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""策略回测器 (2026-09-10, 用户提供策略手册: 爆冷/正路/时段).

六条策略, 等额注(1u/场):
  S1 爆冷独赢   弱胜赔 ∈ [2.9, 6.0] 时买弱队独赢
  S2 爆冷波胆   模型 CS top5 中弱队胜类别比分, 前 2 个各 1u
  S3 正路波胆   模型 CS top5 中强队胜类别比分, 前 2 个各 1u
  S4 过度波胆   模型 CS top5 中总球≥4 的比分, 前 2 个各 1u
  S5 让球+1     弱队受让一球 @≈1.96 (盘口 +1 覆盖: 平局或弱队胜)
  S6 大球时段   开盘大 2.5 只在【周三≥18:00 或 周六全天】入场 (用户时段规律)

用法: python scripts/backtest_strategies.py [--days 45]
输出: 每策略 注数/命中/ROI + S6 时段内外对照。
"""
import argparse
import collections
import datetime
import sqlite3
import sys
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
from analysis.live_goal_probe import _open_gq, _dewater_1x2, _open_1x2_from_snapshots, _open_total_from_snapshots  # noqa
from pipeline.cs_db_match import unified_scoreline  # noqa


def devig(oh, od, oa):
    try:
        inv = [1 / float(oh), 1 / float(od), 1 / float(oa)]
        s = sum(inv)
        return [x / s for x in inv]
    except Exception:
        return None


def main(days=45):
    con = _open_gq()
    rows = con.execute("""
        SELECT m.match_key, m.kickoff, m.score_home, m.score_away
        FROM matches m
        WHERE m.status='finished' AND m.score_home IS NOT NULL
          AND m.kickoff >= datetime('now', ?)
        ORDER BY m.kickoff DESC LIMIT 900""", (f'-{days} day',)).fetchall()
    print(f'样本窗口: 近{days}天, 候选 {len(rows)} 场')

    S = {k: {'bets': 0, 'hits': 0, 'ret': 0.0} for k in ('S1爆冷独赢', 'S2爆冷波胆', 'S3正路波胆', 'S4过度波胆', 'S5让球+1')}
    slot = {'in': {'bets': 0, 'hits': 0, 'ret': 0.0}, 'out': {'bets': 0, 'hits': 0, 'ret': 0.0}}
    n = 0
    for mk, ko, fsh, fsa in rows:
        try:
            h, d, a = _open_1x2_from_snapshots(con, mk)
            if not (h and d and a):
                continue
            pm = devig(h, d, a)
            if not pm:
                continue
            n += 1
            dt = datetime.datetime.fromisoformat(str(ko).replace(' ', 'T'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            wd = dt.weekday()          # 0=周一 ... 2=周三, 5=周六
            hour = dt.hour
            in_slot = (wd == 2 and hour >= 18) or (wd == 5)
            ft_tot = fsh + fsa
            actual = 'home' if fsh > fsa else ('draw' if fsh == fsa else 'away')

            def bet(key, odds, win):
                S[key]['bets'] += 1
                S[key]['ret'] += (odds - 1.0) if win else -1.0
                S[key]['hits'] += 1 if win else 0

            # S1 爆冷独赢: 弱胜赔 ∈ [2.9, 6.0]
            under = 'home' if (h > a) else 'away'
            under_odds = float(h if under == 'home' else a)
            fav_odds = float(a if under == 'home' else h)
            if 2.9 <= under_odds <= 6.0 and fav_odds <= 3.5:
                bet('S1爆冷独赢', under_odds, actual == under)

            # S5 让球+1: 弱队 +1 @1.96
            if 2.9 <= under_odds <= 6.0 and fav_odds <= 3.5:
                margin = (fsh - fsa) if under == 'away' else (fsa - fsh)   # 弱队视角净胜
                cover = margin >= -0  # +1 后: 弱队+1 > 0 ⇔ 净胜 ≥ 0 (平/胜), =0 走水按半计
                if margin == 0 and False:
                    pass
                win_or_push = (fsh == fsa) or ((fsh > fsa) == (under == 'home'))
                # 简化: 弱队+1 覆盖 = 平局或弱队赢 (让一球即输一球内)
                if win_or_push:
                    bet('S5让球+1', 1.96, True)
                else:
                    bet('S5让球+1', 1.96, False)

            # 大球时段 S6: 开盘大 2.5
            ou_line, _T = _open_total_from_snapshots(con, mk, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
            if ou_line == 2.5:
                over_hit = ft_tot > 2.5
                bucket = slot['in'] if in_slot else slot['out']
                bucket['bets'] += 1
                bucket['ret'] += (1.0 if over_hit else -1.0)   # 1X2 风格: 赔率≈2.0 → 赢+1/输-1
                bucket['hits'] += 1 if over_hit else 0

            # CS 类策略: 模型 top5
            dm = unified_scoreline(h=h, d=d, a=a, ou_line=ou_line, current_score='0-0', current_minute=0)
            t5 = (dm or {}).get('top5') or []
            if t5:
                fav = 'home' if pm[0] >= max(pm[1], pm[2]) and pm[0] >= 0.40 else None
                def cls(score):
                    hh, aa = (int(x) for x in score.split('-'))
                    return 'home' if hh > aa else ('draw' if hh == aa else 'away')
                # S2 爆冷波胆: 弱队胜类, 前2
                under_scores = [t for t in t5 if cls(t['score']) == under][:2]
                if len(under_scores) >= 1:
                    for t in under_scores:
                        bet('S2爆冷波胆', 6.0, t['score'] == f'{fsh}-{fsa}')   # 假设均赔 6.0 (样本波胆赔率 5.7-7.6)
                # S3 正路波胆: 强队胜类, 前2
                if fav:
                    fav_scores = [t for t in t5 if cls(t['score']) == fav][:2]
                    for t in fav_scores:
                        bet('S3正路波胆', 7.0, t['score'] == f'{fsh}-{fsa}')   # 假设均赔 7.0 (样本 12-24 区间下沿)
                # S4 过度波胆: 总球 ≥ 4, 前2
                big = [t for t in t5 if sum(int(x) for x in t['score'].split('-')) >= 4][:2]
                for t in big:
                    bet('S4过度波胆', 12.0, t['score'] == f'{fsh}-{fsa}')   # 假设均赔 12
        except Exception:
            continue

    print(f'\n══ 策略回测 (n={n} 场, 1u/注) ══')
    for k, v in S.items():
        if v['bets'] == 0:
            print(f'{k}: 0 注')
            continue
        roi = v['ret'] / v['bets'] * 100
        hit = v['hits'] / v['bets'] * 100
        print(f'{k}: {v["bets"]} 注 | 命中 {v["hits"]} ({hit:.1f}%) | ROI {roi:+.1f}%')
    bi, bo = slot['in'], slot['out']
    print('\n── S6 大球时段验证 (大2.5 命中率: 时段内 vs 外) ──')
    if bi['bets']:
        print(f"  时段内(周三≥18点/周六): {bi['hits']}/{bi['bets']} = {bi['hits']/bi['bets']*100:.1f}%")
    if bo['bets']:
        print(f"  时段外:                 {bo['hits']}/{bo['bets']} = {bo['hits']/bo['bets']*100:.1f}%")
    if bi['bets'] and bo['bets']:
        diff = bi['hits']/bi['bets'] - bo['hits']/bo['bets']
        print(f'  时段增益: {diff*100:+.1f}pp ({"✓ 规律成立" if diff > 0.03 else "△ 未达显著" if abs(diff) <= 0.03 else "✗ 反向"})')
    con.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=45)
    main(int(sys.argv[sys.argv.index('--days') + 1]) if '--days' in sys.argv else 45)
