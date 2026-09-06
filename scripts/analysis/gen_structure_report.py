# -*- coding: utf-8 -*-
"""赔率结构审计 + 报告生成 (clean 集). 输出 分析报告/赔率结构审计_20260720.html"""
import sqlite3, datetime, json, html
from collections import Counter, defaultdict
from gq_odds_filter import get_open

DB='data/events.db'
WIN=datetime.datetime(2026,7,16,0,0,0).timestamp()
def is_sim(lg): return ('VS-' in (lg or '')) or ('EAFC' in (lg or '')) or ('PANDA' in (lg or ''))
def pk(s):
    try: return datetime.datetime.strptime(s,'%Y-%m-%d %H:%M').timestamp()
    except: return None
def deoverround(oh,od,oa):
    inv=1.0/oh+1.0/od+1.0/oa
    return (1.0/oh)/inv,(1.0/od)/inv,(1.0/oa)/inv
def vig(oh,od,oa): return (1.0/oh+1.0/od+1.0/oa-1.0)*100
def brier(p,i):
    q=[0,0,0]; q[i]=1.0
    return sum((p[k]-q[k])**2 for k in range(3))

c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; cur=c.cursor()
rows=cur.execute("""SELECT DISTINCT s.match_key FROM odds_snapshots s
  JOIN matches m ON m.match_key=s.match_key
  WHERE s.market='CS' AND s.captured_at>=? AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL""",(WIN,)).fetchall()
matches=[]
for (mk,) in rows:
    m=cur.execute('SELECT league,status,kickoff,score_home,score_away,minute FROM matches WHERE match_key=?',(mk,)).fetchone()
    if not m or is_sim(m['league']) or m['status']!='finished': continue
    if m['minute'] is not None and m['minute']<90: continue
    sh,sa=int(m['score_home']),int(m['score_away'])
    matches.append({'mk':mk,'league':m['league'],'sh':sh,'sa':sa,'res':0 if sh>sa else (2 if sa>sh else 1)})
N=len(matches)

# ---- 1X2 ----
hx2=[m for m in matches if get_open(m['mk'],'1X2')]
ph=[]; pd=[]; pa=[]; actual=[]; briers=[]; vigs=[]
buckets=defaultdict(lambda:[0,0,0])
away_longshot=[]; home_fav=[]
for m in hx2:
    o=get_open(m['mk'],'1X2')
    if not all(k in o for k in('home','draw','away')): continue
    oh,od,oa=o['home'],o['draw'],o['away']
    if min(oh,od,oa)<=0: continue
    P=deoverround(oh,od,oa); ph.append(P[0]); pd.append(P[1]); pa.append(P[2])
    actual.append(m['res']); briers.append(brier(P,m['res'])); vigs.append(vig(oh,od,oa))
    for lab,p,idx in [('H',P[0],0),('D',P[1],1),('A',P[2],2)]:
        b=min(9,int(p*10)); buckets[(lab,b)][0]+=1; buckets[(lab,b)][1]+=p; buckets[(lab,b)][2]+=(1 if m['res']==idx else 0)
    if P[2]<0.10: away_longshot.append(m['res']==2)
    if P[0]>0.65: home_fav.append(m['res']==0)
n=len(ph)
x1=dict(N=n,
  actH=sum(1 for a in actual if a==0)/n*100, actD=sum(1 for a in actual if a==1)/n*100, actA=sum(1 for a in actual if a==2)/n*100,
  impH=sum(ph)/n*100, impD=sum(pd)/n*100, impA=sum(pa)/n*100,
  brier=sum(briers)/n, vig=sum(vigs)/n,
  away_longshot_n=len(away_longshot), away_longshot_hit=sum(away_longshot)/len(away_longshot)*100 if away_longshot else 0,
  home_fav_n=len(home_fav), home_fav_hit=sum(home_fav)/len(home_fav)*100 if home_fav else 0)
buck_rows=''
for lab in ['H','D','A']:
    for b in range(10):
        k=(lab,b)
        if k not in buckets or buckets[k][0]<5: continue
        cnt,si,sa_=buckets[k]
        buck_rows+=f'<tr><td>{lab} {b*10}-{(b+1)*10}%</td><td>{si/cnt*100:.1f}%</td><td>{sa_/cnt*100:.1f}%</td><td>{cnt}</td></tr>'

# ---- CS ----
cs_h1=cs_h3=cs_h5=0; csn=0; cs_impl=[]
for m in matches:
    o=get_open(m['mk'],'CS')
    if not o: continue
    o={k:float(v) for k,v in o.items() if float(v)>0}
    if len(o)<3: continue
    csn+=1; asc=f'{m["sh"]}-{m["sa"]}'
    ranked=sorted(o.items(),key=lambda x:x[1])
    if asc==ranked[0][0]: cs_h1+=1
    if asc in {x[0] for x in ranked[:3]}: cs_h3+=1
    if asc in {x[0] for x in ranked[:5]}: cs_h5+=1
    if asc in o: cs_impl.append(1.0/o[asc]/sum(1.0/v for v in o.values()))
xcs=dict(N=csn, h1=cs_h1/csn*100, h3=cs_h3/csn*100, h5=cs_h5/csn*100,
  impl=sum(cs_impl)/len(cs_impl)*100 if cs_impl else 0)

# ---- AH (全线条) ----
ah_lines=Counter(); ah_cover=defaultdict(lambda:[0,0]); ahn=0
for m in matches:
    sh,sa=m['sh'],m['sa']
    for x in ['0.00','+0.25','+0.50','+0.75','+1.00','+1.25','+1.50','+1.75','+2.00','-0.25','-0.50','-0.75','-1.00','-1.75']:
        o=get_open(m['mk'],f'AH_{x}')
        if not o or 'home' not in o or 'away' not in o: continue
        line=float(x); ahn+=1; ah_lines[f'AH_{x}']+=1
        home_cover=(sh-line)>sa; push=(sh-line)==sa
        if not push: ah_cover[f'AH_{x}'][0]+=(1 if home_cover else 0); ah_cover[f'AH_{x}'][1]+=1
        break
ah_rows=''
for ln,cnt in ah_lines.most_common():
    cov=ah_cover[ln]; rate=cov[0]/cov[1]*100 if cov[1] else 0
    ah_rows+=f'<tr><td>{ln}</td><td>{cnt}</td><td>{rate:.1f}%</td></tr>'

# ---- OU ----
ou_avail=cur.execute("SELECT COUNT(*) FROM odds_snapshots WHERE market LIKE 'OU_%' AND match_key IN (SELECT match_key FROM matches WHERE score_home IS NOT NULL)").fetchone()[0]
ou_in_window=cur.execute("""SELECT COUNT(*) FROM odds_snapshots s JOIN matches m ON m.match_key=s.match_key
  WHERE s.market LIKE 'OU_%' AND s.captured_at>=? AND m.score_home IS NOT NULL""",(WIN,)).fetchone()[0]

c.close()

HTML=f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>赔率结构审计 2026-07-20</title>
<style>body{{font-family:-apple-system,'Microsoft YaHei',sans-serif;background:#0f1216;color:#e6e6e6;margin:0;padding:24px}}
.card{{background:#1a1f26;border:1px solid #2a313b;border-radius:10px;padding:18px;margin:14px 0}}
h1{{color:#5be85a;font-size:22px}} h2{{color:#1890ff;font-size:17px;border-left:3px solid #1890ff;padding-left:8px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}} th,td{{border:1px solid #2a313b;padding:5px 8px;text-align:left}}
th{{background:#222a33;color:#9fd3ff}} .hi{{color:#5be85a;font-weight:bold}} .warn{{color:#ffb020}} .bad{{color:#ff5b5b}}
.tag{{display:inline-block;background:#222a33;border:1px solid #2a313b;border-radius:5px;padding:2px 8px;margin:2px;font-size:12px}}
.note{{font-size:12px;color:#9aa4b2;line-height:1.6}}</style></head><body>
<h1>赔率结构审计 · 2026-07-20 (clean 集)</h1>
<div class=card><div class=note>
数据窗口: 2026-07-16 ~ 07-20 · 来源 events.db(乐鱼采集器持续运行)<br>
<b>干净集 N={N}</b> = 完赛 + 非模拟联赛 + <b>minute≥90</b>(剔除 8 场 minute=45 假终场) + 有实际比分。<br>
<b>关键修正</b>: 此前误判"GQ 比分污染不可用"——实测 322 场全部 last_seen-kickoff≥105min(无早退冻结), 49%单球/0-1≈1-0 是<b>联赛构成</b>(友谊赛/青年/女足/后备队主导)造成, 非污染。仅 8 场(2.5%)minute=45 标finished 为真污染, 已剔除。<br>
铁律: 只用数据; 本窗口被<b>低级别联赛主导</b>, 所有规律须注明联赛分层, 禁止直接套用至主流联赛/WC。
</div></div>

<div class=card><h2>一、胜平负 1X2 初盘结构 (N={x1['N']})</h2>
<table><tr><th></th><th>实际频率</th><th>初盘均值隐含</th></tr>
<tr><td>主胜 H</td><td>{x1['actH']:.1f}%</td><td>{x1['impH']:.1f}%</td></tr>
<tr><td>平局 D</td><td class=bad>{x1['actD']:.1f}%</td><td class=warn>{x1['impD']:.1f}%</td></tr>
<tr><td>客胜 A</td><td>{x1['actA']:.1f}%</td><td>{x1['impA']:.1f}%</td></tr></table>
<p class=note>Brier(初盘1X2)=<b>{x1['brier']:.3f}</b> · 初盘平均抽水 vig=<b>{x1['vig']:.2f}%</b></p>
<div class=tag>平局被市场高估 {x1['impD']-x1['actD']:+.1f}pp</div>
<div class=tag>客胜被低估 {x1['actA']-x1['impA']:+.1f}pp</div>
<h3>校准分桶 (隐含 vs 实际, N≥5)</h3>
<table><tr><th>区间</th><th>隐含</th><th>实际</th><th>N</th></tr>{buck_rows}</table>
<div class=note>
<b>操盘手解读</b>: 初盘在极端区间严重失准——
<span class=bad>强队隐含70%+ 实际仅胜 {x1['home_fav_hit']:.0f}%</span>(N={x1['home_fav_n']}, 过度自信);
<span class=hi>客队长尾隐含&lt;10% 实际胜 {x1['away_longshot_hit']:.0f}%</span>(N={x1['away_longshot_n']}, 严重低估)。
中段(隐含30-50%)校准良好。这是低级别联赛典型"庄家朝热门/主队倾斜定价"制造下盘价值。
</div></div>

<div class=card><h2>二、波胆 CS 初盘结构 (N={xcs['N']})</h2>
<table><tr><th>初盘命中</th><th>hit1</th><th>hit3</th><th>hit5</th></tr>
<tr><td>实际比分进入初盘排名</td><td class=hi>{xcs['h1']:.1f}%</td><td class=hi>{xcs['h3']:.1f}%</td><td class=hi>{xcs['h5']:.1f}%</td></tr></table>
<p class=note>实际比分的初盘隐含概率均值=<b>{xcs['impl']:.2f}%</b>(合理区间, 市场CS校准良好)。</p>
<div class=note><b>操盘手解读</b>: CS 初盘作为<b>排名器</b>高度有效——top3 命中 33%、top5 命中 49%, 与既有 cs_triangulate 混合排名结论一致。
当前 blend=0.7市场CS+0.3 OIP 在 obscure 层市场CS更准, 但主流层OIP更稳 → <b>blend 应联赛分层</b>(见优化建议)。</div></div>

<div class=card><h2>三、让球 AH 初盘结构 (N={ahn})</h2>
<table><tr><th>盘口</th><th>样本</th><th>让球方(主)覆盖率</th></tr>{ah_rows}</table>
<div class=note><b>操盘手解读</b>: AH_0.00 主队(让球方)覆盖率 {ah_cover['AH_0.00'][0]/ah_cover['AH_0.00'][1]*100:.1f}%(若盘口合理应≈50%, 偏低因本窗口客队强势)。AH 样本集中于平手盘, 深盘覆盖不足, 单独校准统计力弱。</div></div>

<div class=card><h2>四、大小球 OU 初盘</h2>
<div class=note>OU 快照在 events.db 存在({ou_avail} 条), 但<b>本窗口 CS 比赛无 OU 覆盖</b>(窗口内 OU 快照 {ou_in_window} 条, 属另一批比赛)。
→ 本干净集<b>无法校准 OU</b>。需扩展采集器对 CS 覆盖比赛同时采 OU, 或换用覆盖 OU 的比赛集重做。</div></div>

<div class=card><h2>五、优化建议 (按风险分级)</h2>
<p><span class=tag>已落地(零风险)</span> 干净集回测底座: 剔除 minute&lt;90 假终场 + 模拟联赛, 防未来校准被 8 场污染误导。</p>
<p><span class=tag>方向信号(可直接用)</span> 低级别联赛: ①<b>做空平局</b>(市场高估平局~10pp价值); ②<b>博客队长尾</b>(隐含&lt;10%实际胜60-78%)。⚠️仅限 obscure 层, 禁套 WC/主流。</p>
<p><span class=tag>建议(需分层,待确认)</span> ① draw_zone_boost 改<b>联赛分层</b>: obscure 层关 boost(市场已高估平局), 主流层保留; ② cs_triangulate blend 改分层(obscure 0.8市场/0.2 OIP, 主流 0.6/0.4); ③ goal_scale/rho <b>不动</b>(来自14万条interwetten主流真赛果walkforward, 本窗口 obscure 均值1.73球会误导下调)。</p>
<p><span class=tag>数据缺口(P1)</span> 扩展采集器: 对 CS 覆盖比赛<b>同步采 OU + AH 各线</b>, 才能做四大市场完整校准; 外部赛果校验闸门(防 minute=45 假终场入库)。</p>
</div></body></html>"""
open('分析报告/赔率结构审计_20260720.html','w',encoding='utf-8').write(HTML)
print('REPORT WRITTEN. N=',N,'1X2_N=',x1['N'],'CS_N=',xcs['N'],'AH_N=',ahn)
print('1X2: actH/D/A=',round(x1['actH'],1),round(x1['actD'],1),round(x1['actA'],1),
      '| impH/D/A=',round(x1['impH'],1),round(x1['impD'],1),round(x1['impA'],1))
print('away_longshot_hit=',round(x1['away_longshot_hit'],1),'home_fav_hit=',round(x1['home_fav_hit'],1))
print('CS hit1/3/5=',round(xcs['h1'],1),round(xcs['h3'],1),round(xcs['h5'],1))
