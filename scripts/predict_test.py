#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WC2026 预测 — DrawGate v5.4 + Elo + cs_other + OU校准
========================================================
用法:
  python scripts/predict.py 2026-06-27 --db    # 预测+入库
  python scripts/predict.py all --db            # 全部未来比赛
  python scripts/predict.py 2026-06-27 --json   # JSON输出

v5.4 (2026-06-26):
  - DrawGate v5.4: Mode A增强(boost 0.05→0.09, threshold 0.28→0.24)
  - 新增: away_skepticism 客场浅让抑制
  - 新增: group_stage_rotation 小组赛末轮轮换检测  
  - 废弃: Mode B (0%准确率)
  - S7屠杀惩罚: 新增 abs(hcp)≥0.5 门槛
  - Mode C: od_max 6.0→8.5
"""
import sys, math, json, argparse, sqlite3, hashlib
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

ARCH_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

from rules.drawgate_v53 import apply_drawgate, imp_from_odds, detect_match_type
from rules.d_gate_utils import ALL_RESULTS, COVER_DB, STAR_PLAYERS

# ═══════════════════════════════════════
# Data
# ═══════════════════════════════════════

def load_fifa():
    p = ARCH_ROOT / 'config' / 'fifa_rankings_2026.json'
    if not p.exists(): return {}
    with open(p, encoding='utf-8') as f: data = json.load(f)
    data.pop('_meta', None); return data

def load_db(date_str):
    p = ARCH_ROOT / 'data' / 'wc2026_timeline.db'
    if not p.exists(): return []
    conn = sqlite3.connect(str(p)); cur = conn.cursor()
    cur.execute('''SELECT m.id,m.match_date,m.home_team,m.away_team,
        o.cs_other,o.ft_home,o.ft_draw,o.ft_away,o.ft_ah_handicap,o.ft_ou_line
        FROM wc2026_matches m LEFT JOIN wc2026_odds_snapshots o ON m.id=o.match_id
        WHERE m.match_date=?''', (date_str,))
    rows = cur.fetchall(); conn.close()
    return [{'id':r[0],'date':r[1],'home':r[2],'away':r[3],
             'cs':r[4] or 30,'oh':r[5],'od':r[6],'oa':r[7],'hcp':r[8] or 0,'ou':r[9] or 2.5} for r in rows]

# ═══════════════════════════════════════
# Elo
# ═══════════════════════════════════════

def build_elo(fifa):
    fe = lambda t: 2000-(fifa.get(t,100)-1)*6
    elo = {t:fe(t) for t in fifa}; K=32
    for h,a,hg,ag,hcp,_ in ALL_RESULTS:
        rh,ra=elo.get(h,1500),elo.get(a,1500)
        eh=1/(1+10**((ra-rh)/400))
        sh=1.0 if hg>ag else (0.5 if hg==ag else 0)
        gm=1+(abs(hg-ag)-1)*0.5 if abs(hg-ag)>1 else 1
        elo[h]=rh+K*gm*(sh-eh); elo[a]=ra+K*gm*((1-sh)-(1-eh))
    return elo

def build_tstats():
    s={}
    for h,a,hg,ag,_,_ in ALL_RESULTS:
        for t,gf,ga in [(h,hg,ag),(a,ag,hg)]:
            if t not in s: s[t]={'gf':0,'ga':0,'g':0}
            s[t]['gf']+=gf; s[t]['ga']+=ga; s[t]['g']+=1
    return s

def elo_prob(eh,ea):
    d=eh-ea; ph=1/(1+10**(-d/400)); pa=1/(1+10**(d/400))
    pd=0.28*math.exp(-(abs(d)/400)**2); t=ph+pa+pd
    return ph/t,pd/t,pa/t

def elo_hcp(eh,ea):
    d=eh-ea
    if abs(d)<50: return 0
    if abs(d)<150: return round(d/200,2)
    if abs(d)<300: return round(d/133,2)
    return round(d/100,2)

def est_ou(eh,ea,ts,h,a):
    hs=ts.get(h,{'gf':1,'ga':1,'g':1}); as_=ts.get(a,{'gf':1,'ga':1,'g':1})
    b=((hs['gf']+hs['ga'])/max(hs['g'],1)+(as_['gf']+as_['ga'])/max(as_['g'],1))/2
    b+=abs(eh-ea)/200*0.25; return max(2,min(3.5,round(b*2)/2))

# ═══════════════════════════════════════
# Predict
# ═══════════════════════════════════════

def predict(h,a,elo,ts,cs=30,db=None):
    """v5.4: DrawGate调整 + 安全阀裁决"""
    eh,ea=elo.get(h,1500),elo.get(a,1500)
    if db and db.get('oh'):
        oh,od,oa=db['oh'],db['od'],db['oa']; ph,pd,pa=1/oh,1/od,1/oa
        t=ph+pd+pa; ph,pd,pa=ph/t,pd/t,pa/t
    else: ph,pd,pa=elo_prob(eh,ea); oh,od,oa=1/ph,1/pd,1/pa
    hcp=db.get('hcp') if db and db.get('hcp') else elo_hcp(eh,ea)
    ou=db.get('ou',2.5) if db and db.get('ou') else est_ou(eh,ea,ts,h,a)

    # ── v5.4: DrawGate 调整 ──
    imp_h,imp_d,imp_a=imp_from_odds(oh,od,oa)
    dgate=apply_drawgate(imp_h,imp_d,imp_a,
        odds={'home':oh,'draw':od,'away':oa},
        handicap=hcp,ou_line=ou,match_type='tournament')

    # ── v5.5: Multi-Signal Verdict Engine ──
    # 替代旧版纯概率裁决，融合 DrawGate + OU + Handicap 三维信号
    from rules.multi_signal_engine import verdict as ms_verdict
    verdict,ms_reason=ms_verdict(oh,od,oa,hcp,ou,cs_other=cs)
    
    # 保留 DrawGate 输出用于展示
    sig=dgate['triggered_signals'][:]
    if ms_reason!='argmax': sig.append(f'MS:{ms_reason}')
    mode=ms_reason if ms_reason!='argmax' else dgate['dgate_mode']
    db_boost=dgate['draw_boost']

    hc,ac=COVER_DB.get(h,{}),COVER_DB.get(a,{})
    return {'home':h,'away':a,'eh':eh,'ea':ea,'ed':eh-ea,
            'ph':ph,'pd':pd,'pa':pa,'oh':oh,'od':od,'oa':oa,
            'hcp':hcp,'ou':ou,'cs':cs,
            'v':verdict,'m':mode,'db':db_boost,'sig':sig,
            'risk':dgate['risk_tag'],
            'hs':hc.get('style','?'),'as':ac.get('style','?')}

def scores(eh,ea,v,ou=2.5,cs=30,hcp=None,sporttery_hcp=None):
    """v5.5: 四维比分优化 — 1X2方向 × 让球 × 胜平负 × OU"""
    diff=abs(eh-ea)
    base=ou
    if cs<5: base+=0.5
    elif cs>25: base-=0.2
    base+=diff/300
    base=max(2.3,min(5.0,base))
    ratio=1/(1+10**(-(eh-ea)/400))
    lh=base*ratio; la=base-lh
    lh=max(1.2,min(4.8,lh)); la=max(0.5,min(3.5,la))
    s=[]
    for hg in range(8):
        for ag in range(8):
            ph=(lh**hg*math.exp(-lh))/max(math.factorial(hg),1)
            pa=(la**ag*math.exp(-la))/max(math.factorial(ag),1)
            s.append((hg,ag,ph*pa))
    s.sort(key=lambda x:x[2],reverse=True)

    # ── 维度1: 1X2方向过滤 ──
    if v=='D': p=[(h,a,pr) for h,a,pr in s if h==a]
    elif v=='H': p=[(h,a,pr) for h,a,pr in s if h>a]
    else: p=[(h,a,pr) for h,a,pr in s if h<a]

    # ── 维度2: 让球锚点 (让2球不穿律 + 联动铁律) ──
    hcp_eff = sporttery_hcp if sporttery_hcp else hcp
    hcp_abs = abs(hcp_eff) if hcp_eff else 0
    let2ball = hcp_abs >= 1.75  # 竞彩让≥1.75球

    if let2ball:
        # 让2球不穿律: 净胜≤1球 (走水路径)
        p = [(h,a,pr) for h,a,pr in p if abs(h-a) <= 1]
    elif hcp_abs >= 1.0 and ou <= 2.0:
        # 法则1: 深让小球走水 → 0-2, 1-0
        p = [(h,a,pr) for h,a,pr in p if h+a <= 2]
    elif hcp_abs >= 1.0 and ou >= 3.0:
        # 法则2: 深让大球爆冷 → 2-1, 2-2, 1-1 (不过滤, 大球优先)
        p = [(h,a,pr) for h,a,pr in p if h+a >= 2]

    # ── 维度4: OU总进球约束 ──
    if ou <= 2.0:
        # 小球: 总进球≤2优先
        p_small = [(h,a,pr) for h,a,pr in p if h+a <= 2]
        p = p_small if p_small else p
    elif ou >= 3.0:
        # 大球: 总进球≥2优先
        p_large = [(h,a,pr) for h,a,pr in p if h+a >= 2]
        p = p_large if p_large else p

    return {'lh':lh,'la':la,'rec':p[:4],'top':s[:5],
            'let2ball':let2ball,'hcp_abs':hcp_abs,'ou_line':ou}

# ═══════════════════════════════════════
# DB Log
# ═══════════════════════════════════════

def init_db(p):
    conn=sqlite3.connect(str(p))
    conn.execute('''CREATE TABLE IF NOT EXISTS predictions_log(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id INTEGER,match_date TEXT,home TEXT,away TEXT,
        verdict TEXT,score TEXT,
        elo_h REAL,elo_a REAL,ph REAL,pd REAL,pa REAL,
        oh REAL,od REAL,oa REAL,hcp REAL,ou REAL,cs_other REAL,
        mode TEXT,db REAL,signals TEXT,
        predicted_at TEXT,hash TEXT UNIQUE)''')
    conn.commit(); conn.close()

def save(p, pred, sc, md=None):
    conn=sqlite3.connect(str(p)); cur=conn.cursor()
    mid=md.get('id') if md else None
    dt=md.get('date',datetime.now(timezone.utc).strftime('%Y-%m-%d')) if md else datetime.now(timezone.utc).strftime('%Y-%m-%d')
    vh=hashlib.md5(f"{dt}|{pred['home']}|{pred['away']}|{pred['v']}".encode()).hexdigest()[:12]
    r=sc['rec']; ss=f"{r[0][0]}-{r[0][1]} / {r[1][0]}-{r[1][1]}" if len(r)>1 else f"{r[0][0]}-{r[0][1]}"
    cur.execute('''INSERT OR REPLACE INTO predictions_log
        (match_id,match_date,home,away,verdict,score,
         elo_h,elo_a,ph,pd,pa,oh,od,oa,hcp,ou,cs_other,
         mode,db,signals,predicted_at,hash)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (mid,dt,pred['home'],pred['away'],pred['v'],ss,
         round(pred['eh'],1),round(pred['ea'],1),
         round(pred['ph'],4),round(pred['pd'],4),round(pred['pa'],4),
         round(pred['oh'],2),round(pred['od'],2),round(pred['oa'],2),
         pred['hcp'],pred['ou'],pred['cs'],pred['m'],
         round(pred['db'],4),'|'.join(pred['sig'][:4]),
         datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),vh))
    conn.commit(); conn.close()

# ═══════════════════════════════════════
# Output
# ═══════════════════════════════════════

def card(p,sc,fifa):
    h,a=p['home'],p['away']; rh,ra=fifa.get(h,99),fifa.get(a,99)
    if p['v']=='D': v,c='🔥 平局','⭐⭐⭐'
    elif p['v']=='H':
        v,c=f'🏠 {h}胜','⭐⭐⭐⭐⭐' if p['ph']>0.7 else ('⭐⭐⭐⭐' if p['ph']>0.55 else '⭐⭐⭐')
    else:
        v,c=f'✈️ {a}胜','⭐⭐⭐⭐⭐' if p['pa']>0.7 else ('⭐⭐⭐⭐' if p['pa']>0.55 else '⭐⭐⭐')
    r=sc['rec']; p1=f"{r[0][0]}-{r[0][1]} ({r[0][2]:.1%})"
    p2=f"{r[1][0]}-{r[1][1]} ({r[1][2]:.1%})" if len(r)>1 else '—'
    return (
        f"┌{'─'*55}┐\n"
        f"│ {h} #{rh} vs {a} #{ra}  cs={p['cs']:.1f}\n"
        f"├{'─'*55}┤\n"
        f"│ Elo:{p['eh']:.0f} vs {p['ea']:.0f}({p['ed']:+.0f}) OU={p['ou']} HCP={p['hcp']:+.2f}\n"
        f"│ H={p['ph']:.1%} D={p['pd']:.1%} A={p['pa']:.1%}\n"
        f"│ 赔率 H={p['oh']:.2f} D={p['od']:.2f} A={p['oa']:.2f}\n"
        f"│ 风格 {p['hs']} vs {p['as']}\n"
        f"├{'─'*55}┤\n"
        f"│ 🎯 {v:24s} {c}  {p['m']}\n"
        f"│ 首选 {p1:16s} 次选 {p2:16s}\n"
        f"│ {p['sig'][:2] if p['sig'] else '无'}\n"
        f"└{'─'*55}┘"
    )

def scard(p,sc):
    h,a=p['home'],p['away']
    l=[f'\n{h} vs {a} λ_H={sc["lh"]:.1f} λ_A={sc["la"]:.1f}']
    if p['v']=='D': l.append('  → 平局')
    elif p['v']=='H': l.append(f'  → {h}胜')
    else: l.append(f'  → {a}胜')
    for hg,ag,pb in sc['rec']:
        l.append(f'  {hg}-{ag}  {pb:.1%}  {"█"*int(pb*100)}')
    t=sc['top']; l.append(f'  Top:{t[0][0]}-{t[0][1]}({t[0][2]:.1%}) {t[1][0]}-{t[1][1]}({t[1][2]:.1%})')
    return '\n'.join(l)

# ═══════════════════════════════════════
# API积分榜
# ═══════════════════════════════════════

def fetch_group_standings():
    """从API获取分组积分榜, 组名→[球队数据]"""
    try:
        from data_collector.football_data_live import FootballDataLive
        fdl = FootballDataLive()
        data = fdl._request('/competitions/WC/matches?season=2026', cache_ttl=3600)
        matches = [m for m in data.get('matches',[]) if m.get('status')=='FINISHED']
        groups = defaultdict(lambda: defaultdict(lambda: {'pts':0,'gf':0,'ga':0,'mp':0}))
        for m in matches:
            g = m.get('group','?')
            h = m['homeTeam']['name']; a = m['awayTeam']['name']
            sc = m.get('score',{}).get('fullTime',{})
            if not sc or sc.get('home') is None: continue
            hg, ag = sc['home'], sc['away']
            groups[g][h]['gf']+=hg; groups[g][h]['ga']+=ag; groups[g][h]['mp']+=1
            if hg>ag: groups[g][h]['pts']+=3
            elif hg==ag: groups[g][h]['pts']+=1
            groups[g][a]['gf']+=ag; groups[g][a]['ga']+=hg; groups[g][a]['mp']+=1
            if ag>hg: groups[g][a]['pts']+=3
            elif hg==ag: groups[g][a]['pts']+=1
        return dict(groups)
    except Exception as e:
        print(f'[WARN] API standings failed: {e}')
        return {}

def get_match_motivation(home, away, groups):
    """从积分榜推断战意标签"""
    for g in groups.values():
        if home in g and away in g:
            hp = g[home]['pts']; ap = g[away]['pts']
            hg,ag = g[home]['mp'], g[away]['mp']
            if hp >= 6: return f'{home}已出线(小胜即可)'
            if ap >= 6: return f'{away}已出线(小胜即可)'
            if hp == 0 and hg >= 2: return f'{home}已淘汰'
            if ap == 0 and ag >= 2: return f'{away}已淘汰'
            if hp == 3 and ap == 3: return '头名之争(不会冒进)'
            if hp == 3 and ap == 0: return f'{away}拼命局(易爆冷)'
            if hp == 0 and ap == 3: return f'{home}拼命局(易爆冷)'
            if hp == 0 and ap == 0: return '生死战(双方保守)'
            return '正常'
    return '未知'

# ═══════════════════════════════════════
# Main
# ═══════════════════════════════════════

def main():
    ap=argparse.ArgumentParser(description='WC2026 Predictor')
    ap.add_argument('date',help='YYYY-MM-DD or all')
    ap.add_argument('--db',action='store_true',help='Pull from DB')
    ap.add_argument('--json',action='store_true',help='JSON output')
    ap.add_argument('--no-save',action='store_true',help='Skip DB save')
    args=ap.parse_args()

    fifa=load_fifa(); elo=build_elo(fifa); ts=build_tstats()
    dp=ARCH_ROOT/'data'/'wc2026_timeline.db'

    if args.date=='all':
        ms=[]
        if args.db and dp.exists():
            c=sqlite3.connect(str(dp)); cur=c.cursor()
            cur.execute("SELECT DISTINCT match_date FROM wc2026_matches WHERE match_date>='2026-06-22' ORDER BY match_date")
            for (d,) in cur.fetchall(): ms.extend(load_db(d))
            c.close()
    else: ms=load_db(args.date)
    if not ms: print(f"[ERROR] No matches for {args.date}"); sys.exit(1)

    if not args.no_save and dp.exists(): init_db(dp)

    # ══ v5.2.6: 积分战意标签 ══
    from collections import defaultdict
    st_pts=defaultdict(int); st_gf=defaultdict(int); st_ga=defaultdict(int); st_mp=defaultdict(int)
    for h,a,hg,ag,_,_ in ALL_RESULTS:
        for t,gf_,ga_ in [(h,hg,ag),(a,ag,hg)]:
            st_gf[t]+=gf_; st_ga[t]+=ga_; st_mp[t]+=1
            if (t==h and hg>ag) or (t==a and ag>hg): st_pts[t]+=3
            elif hg==ag: st_pts[t]+=1
    # DB completed > 6.22
    if dp.exists():
        c=sqlite3.connect(str(dp)); cur=c.cursor()
        cur.execute("SELECT home_team,away_team,actual_result,actual_score FROM wc2026_matches WHERE actual_result IS NOT NULL")
        for h,a,r,s in cur.fetchall():
            if s:
                try:
                    hg,ag=map(int,s.split('-'))
                except: continue
                for t,gf_,ga_ in [(h,hg,ag),(a,ag,hg)]:
                    st_gf[t]+=gf_; st_ga[t]+=ga_; st_mp[t]+=1
                    if (t==h and r=='H') or (t==a and r=='A'): st_pts[t]+=3
                    elif r=='D': st_pts[t]+=1
        c.close()
    def group_tag(h,a):
        hp=st_pts.get(h,0); ap=st_pts.get(a,0); hm=st_mp.get(h,0); am=st_mp.get(a,0)
        tag='neutral'
        if hp>=4 and ap<=1: tag='safe'       # 强队安全, 小胜即可
        elif hp>=6: tag='safe'                 # 已出线
        elif hm==0 or am==0: tag='neutral'     # 未知
        return tag

    preds=[]
    for m in ms:
        pd_=predict(m['home'],m['away'],elo,ts,m['cs'],
                    {'oh':m['oh'],'od':m['od'],'oa':m['oa'],'hcp':m['hcp'],'ou':m['ou']})
        sc=scores(pd_['eh'],pd_['ea'],pd_['v'],pd_['ou'],pd_['cs'],hcp=pd_['hcp']); pd_['sc']=sc; preds.append((pd_,m))
        if not args.no_save and dp.exists(): save(dp,pd_,sc,m)

    if args.json:
        out=[]
        for p,_ in preds:
            j={k:v for k,v in p.items() if k!='sc'}
            j['scores']={'lh':p['sc']['lh'],'la':p['sc']['la'],
                'rec':[f"{h}-{a}({pb:.1%})" for h,a,pb in p['sc']['rec']],
                'top5':[f"{h}-{a}({pb:.1%})" for h,a,pb in p['sc']['top']]}
            out.append(j)
        print(json.dumps(out,ensure_ascii=False,indent=2))
    else:
        print(f"\n{'比赛':18s} | {'判定':6s} | {'首选':6s} | {'次选':6s} | {'让球':12s} | {'OU':4s}")
        print("-"*70)
        for p,_ in preds:
            r=p['sc']['rec']; p1=f"{r[0][0]}-{r[0][1]}"; p2=f"{r[1][0]}-{r[1][1]}" if len(r)>1 else '—'
            if p['v']=='D': v='平局'
            elif p['v']=='H': v='屠杀' if p['cs']<5 else '主胜'
            else: v='屠杀' if p['cs']<5 else '客胜'
            # 让球策略
            hcp_abs = abs(p.get('hcp',0))
            let2ball = p['sc'].get('let2ball',False)
            if let2ball: hcp_str='让平+让胜'
            elif hcp_abs>=1.0: hcp_str='让胜+让平'
            elif hcp_abs>=0.5: hcp_str='胜+平'
            else: hcp_str='胜+平'
            # OU方向
            ou_line = p['ou']
            ou_str = '小球' if ou_line<=2.0 else ('大球' if ou_line>=3.0 else '中')
            print(f"{p['home']}vs{p['away']:14s} | {v:6s} | {p1:6s} | {p2:6s} | {hcp_str:12s} | {ou_str:4s}")
        print()
        for p,_ in preds:
            print(card(p,p['sc'],fifa)); print(scard(p,p['sc'])); print()
        if not args.no_save: print(f"💾 {len(preds)}场已入库")

if __name__=='__main__': main()
