#!/usr/bin/env python3
"""
哨响AI · 最强信号扫描器 (Signal Scanner)
============================================
从 events.db 扫全部当周比赛，按 tick + OU + 平局 + 冲突检测综合打分排序。

信号权重:
  ±3  tick信号 (主/客 trap -3 / strong +3)
  ±2  OU阈值 (over/under ≤1.75)
  +1  胶着盘 (主客差<0.3 且平赔<3.5)
  +2  双刃 (主强+客陷 或 客强+主陷)

等级:
  ≥5  ⚡⚡⚡ 最强
  ≥3  ⚡⚡ 强
  ≥1  ⚡ 弱

用法: python scripts/signal_scanner.py [--top 15]
"""

import sqlite3, sys

def scan(limit=15):
    c = sqlite3.connect('data/events.db')
    c.row_factory = sqlite3.Row
    rows = c.execute('''
        select m.match_key, m.home, m.away, m.league, m.kickoff, m.status,
               (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='home' order by captured_at desc limit 1) as oh,
               (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='draw' order by captured_at desc limit 1) as od,
               (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='away' order by captured_at desc limit 1) as oa,
               (select odds from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' and selection='over' order by captured_at desc limit 1) as over_o,
               (select odds from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' and selection='under' order by captured_at desc limit 1) as under_o
        from matches m
        where exists (select 1 from odds_snapshots where match_key=m.match_key and market='1X2')
        order by m.kickoff desc limit 200
    ''').fetchall()
    c.close()

    def tick(o):
        return int(round(float(o)*100))%10 if 1.0<=float(o)<1.5 else None

    signals = []
    for r in rows:
        if not r['oh']: continue
        oh=float(r['oh']); od=float(r['od']); oa=float(r['oa'])
        score = 0; tags = []

        ht = tick(oh)
        if ht == 4: score -= 3; tags.append(f'主陷')
        elif ht in (1,2,9): score += 3; tags.append(f'主强.{ht}')

        at = tick(oa)
        if at == 4: score -= 3; tags.append(f'客陷')
        elif at in (1,2,9): score += 3; tags.append(f'客强.{at}')

        if r['over_o']:
            ov=float(r['over_o']); un=float(r['under_o'])
            if ov <= 1.75: score += 2; tags.append('大优')
            if un <= 1.75: score += 2; tags.append('小优')

        gap=abs(oh-oa)
        if gap<0.3 and od<3.5: score += 1; tags.append('平局30%')

        if ht in (1,2,9) and at==4: score += 2; tags.append('双刃:主强客陷')
        if at in (1,2,9) and ht==4: score += 2; tags.append('双刃:客强主陷')

        fav=min({'home':oh,'draw':od,'away':oa},key=lambda k:{'home':oh,'draw':od,'away':oa}[k])
        bname={'home':'主','draw':'平','away':'客'}[fav]
        level = '⚡⚡⚡' if score>=5 else ('⚡⚡' if score>=3 else ('⚡' if score>=1 else '-'))

        signals.append((score, level, r['home'][:16], r['away'][:16], oh, od, oa,
                        fav, ', '.join(tags), r['league'][:20], r['status'] or '?'))

    signals.sort(key=lambda x: -x[0])

    n = min(limit, len(signals))
    print(f'\n{"="*90}')
    print(f'哨响AI · 最强信号 Top {n}  (共 {len(signals)} 场)')
    print(f'{"="*90}')
    for i, s in enumerate(signals[:n]):
        score, level, home, away, oh, od, oa, fav, tags, league, status = s
        print(f'  #{i+1:2} {level:5} [{score:+3}] {home:16} vs {away:16}  '
              f'{oh:.2f}/{od:.2f}/{oa:.2f} {fav} | {tags[:55]}')
        if league: print(f'       {league}')

    strong = sum(1 for s in signals if s[0]>=5)
    medium = sum(1 for s in signals if 3<=s[0]<5)
    weak = sum(1 for s in signals if 1<=s[0]<3)
    none = sum(1 for s in signals if s[0]<1)
    print(f'\n  最强{strong} | 强{medium} | 弱{weak} | 无{none}')
    return 0

if __name__ == '__main__':
    n = int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='--top' else 15
    raise SystemExit(scan(n))
