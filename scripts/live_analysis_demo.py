import sys; sys.path.insert(0,'D:/Architecture')
from scripts.build_odds_vector_library import query_by_odds

# 定价模板常量(从 odds_feature_library.py 同步)
OU_MISSING = {1.00,2.05,2.15,2.18,2.22,2.24,2.27,2.32,2.34,2.37,2.39,2.41,2.43,2.45,2.46,2.48,2.50,2.52,2.54,2.55,2.57,2.59,2.60,2.62,2.64,2.65,2.67,2.68,2.70,2.71,2.73,2.74,2.75,2.76,2.78,2.79,2.80,2.82,2.84,2.85,2.86,2.88,2.89,2.90,2.92,2.93,2.95,2.97,2.99}
OU_OVER_FAV = {1.88,2.25,2.26,2.38,2.40,2.42,2.49,2.63}
OU_UNDER_FAV = {1.45,1.47,1.49,1.56,1.58,1.59,1.61,1.65,1.80,1.95}

m1={'home':1.24,'draw':4.80,'away':9.00,
    'ou':{'OU_3.00':{'over':1.75,'under':1.95},'OU_3.25':{'over':1.95,'under':1.75},'OU_2.50':{'over':1.70,'under':2.15}},
    'cs':{'1-0':6.0,'2-0':7.0,'2-1':8.0,'3-0':11.0,'1-1':8.5,'0-0':12.0}}
m2={'home':1.55,'draw':3.75,'away':5.70,
    'ou':{'OU_1.50':{'over':1.80,'under':1.85},'OU_1.75':{'over':1.85,'under':1.80},'OU_2.00':{'over':1.85,'under':1.85},'OU_2.50':{'over':2.10,'under':1.65}},
    'cs':{'1-0':7.0,'2-0':8.5,'2-1':9.5,'1-1':7.5}}

for name,m in [('基多大学 vs 迪尔芬',m1),('巴拉纳竞技 vs 维多利亚BA',m2)]:
    print('='*60+f'\n哨响AI 赛前分析: {name}\n'+'='*60)
    h=m['home'];d=m['draw'];a=m['away']
    best_key=min({'home':h,'draw':d,'away':a},key=lambda k:{'home':h,'draw':d,'away':a}[k])
    best_odds={'home':h,'draw':d,'away':a}[best_key]
    digit=int(round(best_odds*100))%10
    bname={'home':'主胜','draw':'平局','away':'客胜'}[best_key]

    print(f'\n[1/4] Tick信号: 1X2={h}/{d}/{a}  最看好={bname}={best_odds}')
    if 1.0<=best_odds<1.5:
        if digit in (2,7): print(f'  >> TICK_WINNER 尾数.{digit:02d} 历史79.7% -> 置信+5pp')
        elif digit==4: print(f'  >> TICK_TRAP 尾数.{digit:02d} 历史68.2% -> 置信-7pp')
        else: print(f'  尾数.{digit:02d} 无信号')
    elif best_odds<2.0: print(f'  区间1.5-2.0 弱信号参考')
    else: print(f'  区间{int(best_odds)}+ tick无意义')

    print('\n[2/4] OU异常检测')
    ou=m.get('ou',{})
    for k,v in sorted(ou.items()):
        line=k[3:];over_o=v['over'];under_o=v['under']
        flags=[]
        if round(over_o,2) in OU_MISSING: flags.append('over禁区')
        if round(under_o,2) in OU_MISSING: flags.append('under禁区')
        if round(over_o,2)==1.71: flags.append('标准价1.71')
        if round(over_o,2) in OU_OVER_FAV: flags.append('over偏高')
        if round(under_o,2) in OU_UNDER_FAV: flags.append('under偏低')
        tag='  [!]' if flags else ''
        msg=' / '.join(flags) if flags else '正常'
        print(f'  OU_{line} over={over_o} under={under_o} -> {msg}{tag}')

    print('\n[3/4] 结构近邻')
    ou_items=[(float(k[3:]),v['over'],v['under']) for k,v in ou.items()]
    nbs=query_by_odds(h,d,a,[(0.0,1.95,1.91)],ou_items,m.get('cs',{}),k=8)
    if nbs:
        res={'home':0,'draw':0,'away':0}
        for sim,nb in nbs:
            r=nb.get('result')
            if r in res: res[r]+=1
        total=sum(res.values())
        print(f'  近邻{total}场: 主{res["home"]} 平{res["draw"]} 客{res["away"]}')
        for i,(sim,nb) in enumerate(nbs[:3]):
            nh=nb.get('home','?')[:14]; na=nb.get('away','?')[:14]
            nr=nb.get('result','?'); ns=f'{nb.get("score_home","?")}-{nb.get("score_away","?")}'
            nl=nb.get('league','?')[:20]
            print(f'  #{i+1} sim={sim:.4f} {nh} vs {na} -> {nr}({ns}) [{nl}]')
    else:
        print('  无相似邻居')

    print(f'\n[4/4] 综合判读')
    ticks=[]
    if 1.0<=best_odds<1.5 and digit in (2,7): ticks.append(f'tick_winner +5pp')
    if nbs and total>=3: ticks.append(f'近邻{res["home"]}/{res["draw"]}/{res["away"]}')
    if ticks: print(f'  推荐: {bname} | {" | ".join(ticks)}')
    else: print(f'  推荐: {bname} | 信号不足')
    print()
