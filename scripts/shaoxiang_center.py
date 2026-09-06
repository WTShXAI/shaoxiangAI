#!/usr/bin/env python3
"""
哨响AI · 分析中心 v1.0
======================
合并三大分析器: signal_scanner + quick_analyze + live_analysis_demo

用法: python scripts/shaoxiang_center.py [--top 10]
"""

import sqlite3, sys, collections, math

GQ_DB = 'data/events.db'

def tick(o):
    try: o=float(o); return int(round(o*100))%10 if 1.0<=o<1.5 else None
    except: return None

def poisson_top3(oh, od, oa, ou_over=None, ou_under=None):
    h=1.0/oh; d=1.0/od; a=1.0/oa; s=h+d+a
    hp=h/s; ap=a/s
    # 用OU信号校准进球期望(锚定2.5线)
    scale = 2.5  # 中性: 预期总进球≈2.5
    if ou_over and ou_under:
        ov=float(ou_over); un=float(ou_under)
        if ov <= 1.75: scale = 2.8   # 大优: ↑破2.5
        elif un <= 1.75: scale = 2.0  # 小优: ↓压2.5
    hl=scale*hp; al=scale*ap
    scores=[]
    for hg in range(6):
        for ag in range(6):
            p=((hl**hg)*math.exp(-hl)/max(1,math.factorial(hg)))*((al**ag)*math.exp(-al)/max(1,math.factorial(ag)))
            scores.append(('{}-{}'.format(hg,ag), p))
    scores.sort(key=lambda x:-x[1])
    return ['{}({:.1f}%)'.format(s,p*100) for s,p in scores[:3]]

def query_neighbors(oh, od, oa):
    try:
        sys.path.insert(0,'D:/Architecture')
        from scripts.build_odds_vector_library import query_by_odds
        nbs=query_by_odds(oh,od,oa,k=5,min_sim=0.80)
        if not nbs: return None
        res={'home':0,'draw':0,'away':0}; details=[]
        for sim,nb in nbs:
            r=nb.get('result')
            if r in res: res[r]+=1
        for i,(sim,nb) in enumerate(nbs[:3]):
            nh=nb.get('home','?')[:10]; na=nb.get('away','?')[:10]; nr=nb.get('result','?')
            details.append('{}/{}->{}({:.3f})'.format(nh,na,nr,sim))
        return {'dist':res,'top':details}
    except: return None

def scan_and_analyze(top_n=10):
    c=sqlite3.connect(GQ_DB); c.row_factory=sqlite3.Row
    rows=c.execute('''
        select m.match_key,m.home,m.away,m.league,m.kickoff,m.status,
          (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='home' order by captured_at desc limit 1) oh,
          (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='draw' order by captured_at desc limit 1) od,
          (select odds from odds_snapshots where match_key=m.match_key and market='1X2' and selection='away' order by captured_at desc limit 1) oa,
          (select odds from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' and selection='over' order by captured_at desc limit 1) ovo,
          (select odds from odds_snapshots where match_key=m.match_key and market like 'OU_%' and market not like 'OU_1H%' and market not like 'OU_2H%' and selection='under' order by captured_at desc limit 1) ovu
        from matches m
        where exists(select 1 from odds_snapshots where match_key=m.match_key and market='1X2')
        order by m.kickoff desc limit 300
    ''').fetchall()
    c.close()

    matches=[]
    for r in rows:
        if not r['oh']: continue
        oh=float(r['oh']); od=float(r['od']); oa=float(r['oa'])
        score=3; signals=[]; warns=[]

        o={'home':oh,'draw':od,'away':oa}
        fav=min(o,key=lambda k:o[k]); fav_odds=o[fav]; bname={'home':'主胜','draw':'平局','away':'客胜'}[fav]

        ht=tick(oh); at=tick(oa)
        if ht==4: score-=2; warns.append('主陷.04')
        elif ht in(1,2,9): score+=3; signals.append('主强.{}(+8pp)'.format(ht))
        if at==4: score-=2; warns.append('客陷.04')
        elif at in(1,2,9): score+=3; signals.append('客强.{}(+8pp)'.format(at))

        if r['ovo']:
            ov=float(r['ovo']); un=float(r['ovu'])
            if ov<=1.75: score+=2; signals.append('大优')
            if un<=1.75: score+=2; signals.append('小优')

        gap=abs(oh-oa)
        if fav_odds<1.5:
            dgt=int(round(fav_odds*100))%10
            if dgt==4: score-=2; warns.append('冷门.04(翻车15.3%)')
            elif dgt in(1,2,9): score+=1
        if gap<0.5 and od<3.5: score-=1; warns.append('胶着(冷门62.9%)')

        if ht in(1,2,9) and at==4: score+=2; signals.append('双刃:主强客陷')
        if at in(1,2,9) and ht==4: score+=2; signals.append('双刃:客强主陷')

        matches.append({
            'name':'{} vs {}'.format(r['home'][:16],r['away'][:16]), 'league':r['league'][:20],
            'oh':oh,'od':od,'oa':oa, 'ovo':r['ovo'],'ovu':r['ovu'],
            'score':score,'fav':bname,'fav_odds':fav_odds,'gap':gap,
            'signals':signals,'warns':warns
        })

    matches.sort(key=lambda x:-x['score'])
    lv=lambda s:'\u26a1'*min(3,max(1,s//2+1)) if s>=1 else '-'

    # === 概览 ===
    print('='*80)
    print('哨响AI · 分析中心')
    print('='*80)
    s7=sum(1 for m in matches if m['score']>=7)
    s5=sum(1 for m in matches if m['score']>=5)
    print('扫盘: {}场 | 最强>=7: {} | 强>=5: {}'.format(len(matches),s7,s5))
    print('{:3} {:5} {:4} {:16} {:16} {:18} {:4} {}'.format('','信号','得分','主队','客队','1X2','方向','信号标记'))
    print('-'*80)
    for i,m in enumerate(matches[:min(30,len(matches))]):
        sig=' / '.join(m['signals'][:2]) if m['signals'] else '-'
        wrn=' !!'+','.join(m['warns'][:2]) if m['warns'] else ''
        home,away=m['name'].split(' vs ')
        print('{:3} {:5} [{:+3}] {:16} {:16} {:.2f}/{:.2f}/{:.2f}  {:4} {}{}'.format(i+1,lv(m['score']),m['score'],home,away,m['oh'],m['od'],m['oa'],m['fav'],sig[:45],wrn[:30]))

    # === 精选 Top N 卡片 ===
    print('\n'+'='*80)
    print('精选 Top {}'.format(top_n))
    print('='*80)
    for m in matches[:top_n]:
        print('\n'+'='*78)
        print('  {}  |  {}'.format(m['name'],m['league']))
        stars='\u2605'*min(5,max(1,m['score']//2+1))
        try: ps_str=' / '.join(poisson_top3(m['oh'],m['od'],m['oa'],m['ovo'],m['ovu']))
        except: ps_str='-'
        print('  {} 强度:{:+d}  方向={}({:.2f})  波胆: {}'.format(stars,m['score'],m['fav'],m['fav_odds'],ps_str))
        if m['signals']:
            print('  -> {}'.format(' / '.join(m['signals'])))
        if m['warns']:
            for w in m['warns']:
                print('  !! {}'.format(w))
        try:
            nbs=query_neighbors(m['oh'],m['od'],m['oa'])
            if nbs:
                d=nbs['dist']; t=sum(d.values())
                if t>0:
                    print('  近邻({}): H{} D{} A{} | {}'.format(t,d['home'],d['draw'],d['away'],' | '.join(nbs['top'][:2])))
        except: pass
        verdict=[]
        if m['score']>=7: verdict.append('强烈关注')
        elif m['score']>=5: verdict.append('可考虑')
        if m['warns']: verdict.append('注意风险')
        else: verdict.append('信号干净')
        print('  判读: {}'.format(' / '.join(verdict)))
        print('='*78)

    return 0

if __name__=='__main__':
    n=int(sys.argv[2]) if len(sys.argv)>2 and sys.argv[1]=='--top' else 6
    raise SystemExit(scan_and_analyze(n))
