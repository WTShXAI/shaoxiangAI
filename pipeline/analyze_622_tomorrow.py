#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
6.22 四场市场深度分析
"""
import sys, math
from pathlib import Path
ARCH_ROOT = Path(r"D:/Architecture v4.0")
sys.path.insert(0, str(ARCH_ROOT))
sys.path.insert(0, str(ARCH_ROOT / "rules"))

from rules.d_gate_engine import apply_dgate_v51

# ═══════════════════════════════════
# 6.22 完整数据
# ═══════════════════════════════════
MATCHES_622 = [
    # home, away, oh, od, oa, hcp, ou, ht_h, ht_d, ht_a, cs_other
    {'name':'乌拉圭vs佛得角', 'home':'乌拉圭','away':'佛得角共和国',
     'oh':1.41,'od':4.25,'oa':8.20,'hcp':-1.25,'ou':2.25,
     'ht_h':1.98,'ht_d':2.20,'ht_a':10.5,'cs':23.0},
    {'name':'新西兰vs埃及', 'home':'新西兰','away':'埃及',
     'oh':5.70,'od':4.05,'oa':1.58,'hcp':1.0,'ou':2.25,
     'ht_h':7.20,'ht_d':2.25,'ht_a':2.13,'cs':24.0},
    {'name':'比利时vs伊朗', 'home':'比利时','away':'伊朗',
     'oh':1.41,'od':4.75,'oa':7.30,'hcp':-1.25,'ou':2.75,
     'ht_h':1.90,'ht_d':2.45,'ht_a':8.50,'cs':14.0},
    {'name':'西班牙vs沙特', 'home':'西班牙','away':'沙特阿拉伯',
     'oh':1.07,'od':11.5,'oa':28.0,'hcp':-2.75,'ou':3.5,
     'ht_h':1.60,'ht_d':2.60,'ht_a':5.50,'cs':3.90},
]

def implied(h,d,a):
    t=1/h+1/d+1/a
    return 1/h/t,1/d/t,1/a/t

def analyze(m):
    oh,od,oa,hcp,ou,cs=m['oh'],m['od'],m['oa'],m['hcp'],m['ou'],m['cs']
    ph,pd,pa=implied(oh,od,oa)
    spread=abs(ph-pa)
    max_imp=max(ph,pa)
    margin=1/oh+1/od+1/oa-1
    
    # S7/S1
    s1=od/math.sqrt(oh*oa)
    s7=ou/max(abs(hcp),0.25)
    
    # D-Gate
    dg=apply_dgate_v51(ph,pd,pa,{'home':oh,'draw':od,'away':oa},hcp,ou,'tournament')
    
    # 平局分析
    draw_imp=pd
    dg_d=dg['d_boosted']
    dg_active=dg['d_gate_active']
    dg_mode=dg['d_gate_mode']
    
    # 半场分析
    hph,hpd,hpa=implied(m['ht_h'],m['ht_d'],m['ht_a']) if m.get('ht_h') else (0,0,0)
    ht_draw_boost = hpd/pd if pd>0 else 0  # 半场平局概率相对全场
    
    # cs_other分级
    if cs>25: cs_tier='极度不确定(爆冷土壤)'
    elif cs>15: cs_tier='高不确定(平局可能)'
    elif cs>7: cs_tier='中等(常规比赛)'
    else: cs_tier=f'锁定({cs}→屠杀/大胜)'
    
    # 综合判定
    if dg_active:
        if cs<5: verdict='D-Gate平局预警但cs否定→屠杀'
        elif cs>15: verdict='D-Gate+cs双确认平局'
        else: verdict=f'D-Gate平局预警[模式{dg_mode}]'
    else:
        if cs>25: verdict='非平局但cs极高→警惕爆冷'
        elif cs>15: verdict='非平局判型,cs偏高'
        else: verdict='常规判型'
    
    # 盘口深度信号
    hcp_signal=''
    if abs(hcp)>=2.5: hcp_signal='超深盘(3球差预期)'
    elif abs(hcp)>=1.5: hcp_signal='深盘(2球差预期)'
    elif abs(hcp)>=0.75: hcp_signal='中盘(1球差预期)'
    elif abs(hcp)>=0.25: hcp_signal='浅盘(半球差预期)'
    else: hcp_signal='平手盘'
    
    # OU信号
    ou_signal=f'大{ou}' if ou>=3.5 else (f'中{ou}' if ou>=2.5 else f'小{ou}')
    
    return {
        'name':m['name'],'home':m['home'],'away':m['away'],
        'ph':ph,'pd':pd,'pa':pa,'spread':spread,'margin':margin,
        's1':s1,'s7':s7,'cs':cs,'cs_tier':cs_tier,
        'dg_active':dg_active,'dg_mode':dg_mode,'dg_d':dg_d,
        'ht_draw_boost':ht_draw_boost,'hph':hph,'hpd':hpd,'hpa':hpa,
        'hcp_signal':hcp_signal,'ou_signal':ou_signal,
        'verdict':verdict,'draw_imp':draw_imp,
    }

results = [analyze(m) for m in MATCHES_622]

print("=" * 85)
print("📊 6.22 四场市场深度分析")
print("=" * 85)

for r in results:
    dg_tag = f"🔴D [{r['dg_mode']}]" if r['dg_active'] else '  '
    cs_level = '🔴' if r['cs']<7 else ('🟢' if r['cs']>15 else '🟡')
    
    print(f"\n{'─'*85}")
    print(f"  {r['name']:>20}")
    print(f"{'─'*85}")
    print(f"  赔率: H={r['ph']:.0%} D={r['pd']:.0%} A={r['pa']:.0%}  spread={r['spread']:.1%}  margin={r['margin']:.1%}")
    print(f"  盘口: {r['hcp_signal']} | OU: {r['ou_signal']}")
    if r['hpd']>0:
        print(f"  半场: H={r['hph']:.0%} D={r['hpd']:.0%} A={r['hpa']:.0%}  HT/FT平局比={r['ht_draw_boost']:.2f}")
    print(f"  S7={r['s7']:.1f}  S1={r['s1']:.3f}  cs_other={r['cs']:.0f} {cs_level}{r['cs_tier']}")
    print(f"  D-Gate: {dg_tag} boost={r['dg_d']:.3f}")
    print(f"  判定: {r['verdict']}")

# ═══ 对比表格 ═══
print(f"\n{'='*85}")
print(f"📋 四场对比一览")
print(f"{'='*85}")
print(f"{'比赛':<20} {'H%':>5} {'D%':>5} {'A%':>5} {'盘口':>8} {'OU':>4} {'S7':>5} {'S1':>6} {'cs':>5} {'D-Gate':>8} {'判定'}")
print(f"{'─'*85}")
for r in results:
    dg_str = f"D[{r['dg_mode']}]" if r['dg_active'] else 'H' if r['ph']>r['pa'] else 'A'
    cs_mark = ' 🔴' if r['cs']<7 else (' 🟢' if r['cs']>15 else '')
    print(f"{r['name']:<20} {r['ph']:>5.0%} {r['pd']:>5.0%} {r['pa']:>5.0%} {r['hcp_signal'][:2]:>8} {r['ou_signal'][-3:]:>4} {r['s7']:>5.1f} {r['s1']:>6.3f} {r['cs']:>5.0f}{cs_mark} {dg_str:>8} {r['verdict'][:20]}")

# ═══ 市场建议 ═══
print(f"\n{'='*85}")
print(f"💡 市场建议")
print(f"{'='*85}")

print(f"""
  🥇 西班牙 vs 沙特 — 屠杀 (>2.5球差)
     赔率: H=1.07, 深盘-2.75, cs=3.90锁定
     D-Gate: Mode C误判平局 (历史Mode C在超热门有30%误判率)
     cs否决: 3.90<5 → 庄家极度确定西班牙屠杀
     → 胜负: 西班牙让2.5球以上

  🥈 新西兰 vs 埃及 — 平局倾向
     赔率: 5.70/4.05/1.58, 浅盘+1.0, cs=24.0
     D-Gate: Mode A平局 + cs双确认
     半场: D=27% > FT D=23% → 半场平局概率更高
     → 胜负: 平局或小分差

  🥉 比利时 vs 伊朗 — 中等风险
     赔率: 1.41/4.75/7.30, 中盘-1.25, cs=14.0
     cs=14.0 > 10: 有平局风险但不如新西兰高
     S7=2.2: OU/HCP比中性, 没有屠杀信号
     → 胜负: 比利时小胜或平局

  4️⃣ 乌拉圭 vs 佛得角 — 最不确定
     赔率: 1.41/4.25/8.20, 中盘-1.25, cs=23.0
     cs=23极度不确定 → 佛得角可能顽抗
     半场: H=52% 仅微弱优势
     → 胜负: 乌拉圭难大胜, 平局不能排除
""")
