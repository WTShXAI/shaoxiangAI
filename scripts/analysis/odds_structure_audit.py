# -*- coding: utf-8 -*-
"""
赔率结构审计 (clean 集, 剔除 minute<90 污染 + 模拟联赛)
基于: 实际终场比分(GQ.matches, 已验证可信) + 初盘赔率(odds_snapshots per-selection 首见)
输出: 1X2 / AH / OU / CS 四大市场 初盘隐含概率 vs 实际频率 校准
铁律: 只用数据; 剔除污染; 不基于噪声上调参
"""
import sqlite3, datetime, json, math
from collections import Counter, defaultdict

DB = 'data/events.db'
WIN = datetime.datetime(2026, 7, 16, 0, 0, 0).timestamp()

def is_sim(lg):
    return ('VS-' in (lg or '')) or ('EAFC' in (lg or '')) or ('PANDA' in (lg or ''))

def pk(s):
    try: return datetime.datetime.strptime(s, '%Y-%m-%d %H:%M').timestamp()
    except: return None

# ---- 复用 gq_odds_filter 取初盘 ----
from gq_odds_filter import get_open

def deoverround(oh, od, oa):
    """去水 -> 隐含概率. 返回 (ph,pd,pa)."""
    inv = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/inv, (1.0/od)/inv, (1.0/oa)/inv

def vig(oh, od, oa):
    return (1.0/oh + 1.0/od + 1.0/oa - 1.0) * 100

def brier(p, outcome_idx):
    """p: [ph,pd,pa], outcome_idx 0/1/2"""
    q = [0.0,0.0,0.0]; q[outcome_idx]=1.0
    return sum((p[i]-q[i])**2 for i in range(3))

c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
cur = c.cursor()

# ---- 收集 clean 比赛 ----
rows = cur.execute('''SELECT DISTINCT s.match_key FROM odds_snapshots s
  JOIN matches m ON m.match_key=s.match_key
  WHERE s.market="CS" AND s.captured_at>=? AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL''', (WIN,)).fetchall()

matches = []
for (mk,) in rows:
    m = cur.execute('SELECT league,status,kickoff,score_home,score_away,minute FROM matches WHERE match_key=?', (mk,)).fetchone()
    if not m or is_sim(m['league']) or m['status'] != 'finished': continue
    if m['minute'] is not None and m['minute'] < 90: continue  # 剔除 8 场污染(minute=45)
    sh, sa = int(m['score_home']), int(m['score_away'])
    if sh + sa == 0 and m['minute'] is not None and m['minute'] < 90: continue
    matches.append({'mk': mk, 'league': m['league'], 'sh': sh, 'sa': sa,
                    'res': 0 if sh>sa else (2 if sa>sh else 1)})

print(f'=== CLEAN 比赛集: N={len(matches)} (已剔除 minute<90 污染 + 模拟联赛) ===\n')

# ============ 1X2 校准 ============
print('################ 1X2 (胜平负) 初盘校准 ################')
hx2 = [m for m in matches if get_open(m['mk'],'1X2')]
print(f'有 1X2 初盘: {len(hx2)} 场')
ph_list=[]; pd_list=[]; pa_list=[]; actual=[]; briers=[]; vigs=[]
bins = defaultdict(lambda: [0,0,0])  # (lo,hi)->[count, sum_implied, sum_actual]
for m in hx2:
    o = get_open(m['mk'],'1X2')
    if not ('home' in o and 'draw' in o and 'away' in o): continue
    oh,od,oa = o['home'],o['draw'],o['away']
    if min(oh,od,oa)<=0: continue
    ph,pd,pa = deoverround(oh,od,oa)
    ph_list.append(ph); pd_list.append(pd); pa_list.append(pa)
    actual.append(m['res']); briers.append(brier([ph,pd,pa], m['res'])); vigs.append(vig(oh,od,oa))
    # 按 implied P(H) 分桶校准
    for lab,p,idx in [('H',ph,0),('D',pd,1),('A',pa,2)]:
        b = min(9, int(p*10))
        key=(lab,b)
        bins[key][0]+=1; bins[key][1]+=p; bins[key][2]+=(1 if m['res']==idx else 0)

n=len(ph_list)
print(f'样本 N={n}')
print(f'实际频率: H={sum(1 for a in actual if a==0)/n*100:.1f}%  D={sum(1 for a in actual if a==1)/n*100:.1f}%  A={sum(1 for a in actual if a==2)/n*100:.1f}%')
print(f'初盘均值隐含: H={sum(ph_list)/n*100:.1f}%  D={sum(pd_list)/n*100:.1f}%  A={sum(pa_list)/n*100:.1f}%')
print(f'Brier(初盘1X2)={sum(briers)/n:.4f}  (0=完美,0.33=随机)')
print(f'初盘平均抽水(vig)={sum(vigs)/n:.2f}%')
print('\n-- 校准分桶 (隐含概率 vs 实际频率, 每10%一桶) --')
for lab in ['H','D','A']:
    print(f'  [{lab}]')
    for b in range(10):
        key=(lab,b)
        if key not in bins or bins[key][0]<5: continue
        cnt,si,sa_=bins[key]
        print(f'    {b*10:2d}-{(b+1)*10:2d}%: 隐含={si/cnt*100:4.1f}%  实际={sa_/cnt*100:4.1f}%  N={cnt}')

# ============ CS 校准 ============
print('\n################ CS (波胆) 初盘校准 ################')
cs_hit1=cs_hit3=cs_hit5=0; cs_n=0
cs_implied_actual=[]  # 实际比分的初盘隐含概率
for m in matches:
    o = get_open(m['mk'],'CS')
    if not o: continue
    # 清洗 odds>0
    o = {k:float(v) for k,v in o.items() if float(v)>0}
    if len(o)<3: continue
    cs_n+=1
    actual_score=f'{m["sh"]}-{m["sa"]}'
    # 按概率(低odds=高概率)排序
    ranked=sorted(o.items(), key=lambda x:x[1])  # 升序 odds = 最可能在前
    top1=ranked[0][0]; top3={x[0] for x in ranked[:3]}; top5={x[0] for x in ranked[:5]}
    if actual_score==top1: cs_hit1+=1
    if actual_score in top3: cs_hit3+=1
    if actual_score in top5: cs_hit5+=1
    if actual_score in o:
        cs_implied_actual.append(1.0/o[actual_score] / sum(1.0/v for v in o.values()))
print(f'样本 N={cs_n}')
print(f'初盘命中: hit1={cs_hit1/cs_n*100:.1f}%  hit3={cs_hit3/cs_n*100:.1f}%  hit5={cs_hit5/cs_n*100:.1f}%')
if cs_implied_actual:
    print(f'实际比分的初盘隐含概率 均值={sum(cs_implied_actual)/len(cs_implied_actual)*100:.2f}%  (若市场无偏应≈ 1/cs_n={100/cs_n:.2f}%)')

# ============ AH / OU (样本较小) ============
print('\n################ AH (让球) / OU (大小球) 初盘校准 ################')
ah_markets = [f'AH_{x}' for x in ['0.00','+0.25','+0.50','+0.75','+1.00','-0.25','-0.50','-0.75','+1.25','+1.50','-1.00','+1.75','-1.75','+2.00']]
ou_markets = [f'OU_{x}' for x in ['1.50','1.75','2.00','2.25','2.50','2.75','3.00','3.25','3.50','3.75','4.00','4.50','5.00']]
ah_hit=ah_n=0; ou_hit=ou_n=0
ah_detail=Counter()
for m in matches:
    sh,sa=m['sh'],m['sa']
    for mk_mkt in ah_markets:
        o=get_open(m['mk'],mk_mkt)
        if not o or 'home' not in o or 'away' not in o: continue
        # 解析让球线
        try: line=float(mk_mkt.split('_')[1])
        except: continue
        # home 让 line 球: home AH 胜 = sh - line > sa
        home_cover = (sh - line) > sa
        push = (sh - line) == sa
        ah_n+=1
        if not push and home_cover: ah_hit+=1
        ah_detail[mk_mkt]+=1
        break
    for mk_mkt in ou_markets:
        o=get_open(m['mk'],mk_mkt)
        if not o or 'over' not in o or 'under' not in o: continue
        try: line=float(mk_mkt.split('_')[1])
        except: continue
        total=sh+sa
        ou_n+=1
        if total>line: ou_hit+=1  # over
        break
print(f'AH: 有初盘 N={ah_n} 场, 主队(让球方)覆盖率={ah_hit/ah_n*100:.1f}% (若让球合理应≈50%)')
print(f'  各线样本: {dict(ah_detail)}')
print(f'OU: 有初盘 N={ou_n} 场, 大球率={ou_hit/ou_n*100:.1f}%')

c.close()
print('\n=== DONE ===')
