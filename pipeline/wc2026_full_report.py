#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WC2026 综合预测报告: D-Gate v5.1 + Elo + cs_other + S7/S1
============================================================
结合四大信号体系为36场未赛生成逐场预测
"""
import sys, os, math, json, warnings, sqlite3
from pathlib import Path
from collections import defaultdict
warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture v4.0")
FAI_ROOT = Path(r"D:/AI/footballAI")

def load_cs_other():
    conn = sqlite3.connect(str(ARCH_ROOT / "data" / "wc2026_timeline.db"))
    rows = conn.execute("""
        SELECT m.match_date, m.home_team, m.away_team, COALESCE(s.cs_other, 0)
        FROM wc2026_matches m
        LEFT JOIN (SELECT match_id, MAX(cs_other) as cs_other FROM wc2026_odds_snapshots GROUP BY match_id) s
        ON s.match_id = m.id ORDER BY m.match_date, m.id
    """).fetchall()
    conn.close()
    cs = {}
    for d, h, a, v in rows:
        if v and v > 0:
            cs[f'{h}vs{a}'] = v
    return cs

CS_OTHER = load_cs_other()

# ═══ 36场未赛完整数据 ═══
FUTURE = [
    # date, home, away, oh, od, oa, hcp, ou
    ['6.22','乌拉圭','佛得角共和国',1.44,4.25,6.30,-1.25,2.5],
    ['6.22','新西兰','埃及',4.55,3.35,1.76,0.75,2.5],
    ['6.22','比利时','伊朗',1.39,4.50,7.10,-1.5,2.5],
    ['6.22','西班牙','沙特阿拉伯',1.08,8.80,18.0,-2.5,3.5],
    ['6.23','挪威','塞内加尔',2.14,3.40,3.10,0.0,2.5],
    ['6.23','法国','伊拉克',1.08,8.80,20.0,-2.5,3.5],
    ['6.23','约旦','阿尔及利亚',6.20,4.15,1.46,1.0,2.5],
    ['6.23','阿根廷','奥地利',1.60,3.85,5.00,-0.5,2.5],
    ['6.24','哥伦比亚','民主刚果',1.44,4.05,6.90,-1.0,2.5],
    ['6.24','巴拿马','克罗地亚',5.70,3.95,1.52,1.0,2.5],
    ['6.24','英格兰','加纳',1.30,5.00,8.30,-1.5,2.5],
    ['6.24','葡萄牙','乌兹别克斯坦',1.22,5.90,10.0,-1.75,3.0],
    ['6.25','南非','韩国',4.95,3.80,1.61,0.75,2.5],
    ['6.25','捷克','墨西哥',4.25,3.35,1.81,-0.5,2.5],
    ['6.25','摩洛哥','海地',1.34,4.85,7.50,-1.5,2.75],
    ['6.25','波黑','卡塔尔',1.61,3.75,5.00,-0.5,2.5],
    ['6.25','瑞士','加拿大',2.12,3.25,3.25,-0.25,2.25],
    ['6.25','苏格兰','巴西',6.90,4.50,1.40,1.5,2.5],
    ['6.26','厄瓜多尔','德国',4.45,3.55,1.72,-0.75,2.5],
    ['6.26','土耳其','美国',2.60,3.50,2.41,0.0,2.5],
    ['6.26','巴拉圭','澳大利亚',2.09,3.20,3.40,-0.25,2.25],
    ['6.26','库拉索','科特迪瓦',11.0,5.80,1.21,-1.75,2.75],
    ['6.26','日本','瑞典',2.11,3.30,3.25,-0.25,2.5],
    ['6.26','突尼斯','荷兰',5.80,4.15,1.49,-1.0,2.5],
    ['6.27','乌拉圭','西班牙',4.70,3.90,1.63,-0.75,2.5],
    ['6.27','佛得角共和国','沙特阿拉伯',2.47,3.35,2.62,0.0,2.25],
    ['6.27','埃及','伊朗',2.16,3.00,3.40,-0.25,2.0],
    ['6.27','塞内加尔','伊拉克',1.40,4.40,7.00,-1.25,2.5],
    ['6.27','挪威','法国',4.05,3.55,1.80,-0.75,2.5],
    ['6.27','新西兰','比利时',9.00,5.20,1.28,-1.5,2.75],
    ['6.28','克罗地亚','加纳',1.62,3.75,5.00,-0.75,2.5],
    ['6.28','哥伦比亚','葡萄牙',3.25,3.30,2.11,-0.25,2.25],
    ['6.28','巴拿马','英格兰',9.10,5.40,1.27,-1.5,2.75],
    ['6.28','民主刚果','乌兹别克斯坦',2.27,3.25,2.97,0.0,2.25],
    ['6.28','约旦','阿根廷',12.0,6.30,1.18,-2.0,3.0],
    ['6.28','阿尔及利亚','奥地利',3.30,3.25,2.11,-0.25,2.25],
]

# ═══ Elo (基于34场赛果) ═══
COMPLETED_RESULTS = [
    ('加拿大','波黑',1,1),('美国','巴拉圭',4,1),('卡塔尔','瑞士',1,1),
    ('巴西','摩洛哥',1,1),('海地','苏格兰',0,1),('澳大利亚','土耳其',2,0),
    ('德国','库拉索',7,1),('瑞典','突尼斯',5,1),('科特迪瓦','厄瓜多尔',1,0),
    ('荷兰','日本',2,2),('伊朗','新西兰',2,2),('比利时','埃及',1,1),
    ('沙特阿拉伯','乌拉圭',1,1),('西班牙','佛得角共和国',0,0),('伊拉克','挪威',1,4),
    ('奥地利','约旦',3,1),('法国','塞内加尔',3,1),('阿根廷','阿尔及利亚',3,0),
    ('乌兹别克斯坦','哥伦比亚',1,3),('加纳','巴拿马',1,0),('英格兰','克罗地亚',4,2),
    ('葡萄牙','民主刚果',1,1),('加拿大','卡塔尔',6,0),('墨西哥','韩国',1,0),
    ('捷克','南非',1,1),('瑞士','波黑',4,1),('土耳其','巴拉圭',2,0),
    ('巴西','海地',3,0),('美国','澳大利亚',2,0),('苏格兰','摩洛哥',0,1),
    ('厄瓜多尔','库拉索',0,0),('德国','科特迪瓦',2,1),('突尼斯','日本',1,5),
    ('荷兰','瑞典',5,1),
]

def compute_elo():
    elo = defaultdict(lambda: 1500)
    for _ in range(15):
        for h, a, hg, ag in COMPLETED_RESULTS:
            diff = elo[h] - elo[a]
            exp = 1/(1+10**(-diff/400))
            act = 1.0 if hg>ag else (0.0 if ag>hg else 0.5)
            k = max(16, 32/(1+abs(hg-ag)*0.15))
            elo[h] += k*(act-exp)
            elo[a] -= k*(act-exp)
    return elo

ELO = compute_elo()

# ═══ D-Gate v5.1 核心函数 ═══
def dgate_v51(ph, pd, pa, oh, od, oa, hcp, ou):
    spread = abs(ph-pa)
    max_imp = max(ph, pa)
    s1 = od/math.sqrt(oh*oa)
    s7 = ou/max(abs(hcp),0.25)
    
    # Mode C: >=70%
    if max_imp >= 0.70:
        d = pd*1.08
        d *= 2.2 if (max_imp>0.75 or abs(hcp)>=1.75) else 1.8
        if od>9.5 and ou>=3.5 and abs(hcp)>=2.5: d*=0.3
        elif od>9.5 and abs(hcp)>=2.5: d*=0.5
        if d>0.14: return 'D', 'C', d, s1, s7
    
    # Mode C-away
    if pa>0.65 and max_imp<0.70:
        d = pd*1.08*2.0
        if d>0.14: return 'D', 'C-away', d, s1, s7
    
    # Mode A: 48-70%
    if 0.48<=max_imp<=0.70:
        d = pd*1.08
        d *= max(0.80, 1-spread*0.30)
        if ou<=2.5: d*=1.05
        if s7>=3.5 and s1<1.30: d*=0.70
        if d>0.28: return 'D', 'A', d, s1, s7
    
    # Mode B
    if spread<0.15:
        d = pd*1.08*1.20
        if d>0.43: return 'D', 'B', d, s1, s7
    
    # Default
    d = pd*1.08
    if spread>0.40: d*=0.70
    elif spread>0.20: d*=0.85
    if s7>=3.5 and s1<1.30: d*=0.70
    if d>0.32: return 'D', 'default', d, s1, s7
    
    return ('H' if ph>pa else 'A'), 'normal', d, s1, s7

# ═══ 综合判定 ═══
def analyze_match(m):
    date, home, away, oh, od, oa, hcp, ou = m
    
    # 基础概率
    total = 1/oh+1/od+1/oa
    imp_h = (1/oh)/total; imp_d = (1/od)/total; imp_a = (1/oa)/total
    
    # Elo
    h_elo = ELO.get(home, 1500)
    a_elo = ELO.get(away, 1500)
    elo_diff = h_elo - a_elo
    elo_home = 1/(1+10**(-elo_diff/400))
    
    # D-Gate
    verdict, mode, d_boost, s1, s7 = dgate_v51(imp_h, imp_d, imp_a, oh, od, oa, hcp, ou)
    
    # cs_other
    key = f'{home}vs{away}'
    cs = CS_OTHER.get(key, 0)
    
    # ── 综合评级 ──
    signals = []
    confidence = 'medium'
    
    if verdict == 'D':
        signals.append('D-Gate:平局预警')
        # cs验证
        if cs > 15:
            signals.append(f'cs={cs:.0f}:高不确定→确认')
            confidence = 'high'
        elif cs > 7:
            signals.append(f'cs={cs:.0f}:中等→维持')
        elif cs > 2:
            signals.append(f'cs={cs:.0f}:锁定→矛盾!')
            confidence = 'low'
        else:
            signals.append('cs缺失')
    
    # Elo偏差
    elo_gap = imp_h - elo_home
    if abs(elo_gap) > 0.20:
        if elo_gap > 0:
            signals.append(f'赔率高估主队{elo_gap:.0%}')
        else:
            signals.append(f'赔率低估主队{abs(elo_gap):.0%}')
    
    # S7/S1
    if s7 > 4.0 and s1 > 1.30:
        signals.append(f'S7={s7:.1f}:屠杀预警')
    elif s7 > 4.0 and s1 < 1.30:
        signals.append(f'S7={s7:.1f}+低S1:强屠杀')
    
    # 最终判定
    if verdict == 'D' and cs > 15:
        final = 'D'  # D-Gate + cs确认 → 平局
    elif verdict == 'D' and cs < 5:
        final = 'H' if imp_h > imp_a else 'A'  # D-Gate平局但cs否定 → 屠杀
    elif mode in ('C','C-away') and s7 > 4.0 and s1 > 1.30:
        final = 'H' if imp_h > imp_a else 'A'  # Mode C + 屠杀信号
    else:
        final = verdict
    
    return {
        'date': date, 'home': home, 'away': away,
        'imp_h': imp_h, 'imp_d': imp_d, 'imp_a': imp_a,
        'elo_h': h_elo, 'elo_a': a_elo, 'elo_home': elo_home,
        'verdict': verdict, 'mode': mode, 'd_boost': d_boost,
        'cs': cs, 's1': s1, 's7': s7,
        'signals': signals, 'confidence': confidence,
        'final': final,
    }

def main():
    results = [analyze_match(m) for m in FUTURE]
    
    # ═══ 输出 ═══
    print("=" * 100)
    print("⚽ WC2026 综合预测报告 — D-Gate v5.1 + Elo + cs_other + S7/S1")
    print("=" * 100)
    
    # 按日期分组
    by_date = defaultdict(list)
    for r in results:
        by_date[r['date']].append(r)
    
    vmap = {'H':'主胜','D':'平局','A':'客胜','?':'?'}
    
    for date in sorted(by_date.keys()):
        matches = by_date[date]
        print(f"\n{'─'*100}")
        print(f"📅 {date} ({len(matches)}场)")
        print(f"{'─'*100}")
        
        for r in matches:
            mode_tag = f"[{r['mode']}]" if r['mode'] != 'normal' else ''
            dg_tag = '🔴D' if r['verdict']=='D' else '  '
            cs_val = r['cs']
            cs_str = f'cs={cs_val:.0f}' if cs_val > 0 else 'cs=?'
            
            # cs解读
            if r['cs'] > 25: cs_note = '极度不确定'
            elif r['cs'] > 15: cs_note = '高不确定'
            elif r['cs'] > 5: cs_note = '中等'
            elif r['cs'] > 0: cs_note = '锁定'
            else: cs_note = ''
            
            # 冲突标记
            conflict = ''
            if r['verdict'] == 'D' and r['cs'] < 5 and r['cs'] > 0:
                conflict = '⚡D-Gate平局vs cs屠杀'
            
            print(f"  {dg_tag} {r['home']:<12} vs {r['away']:<12} "
                  f"H={r['imp_h']:.0%} D={r['imp_d']:.0%} "
                  f"Elo:{r['elo_h']:.0f}-{r['elo_a']:.0f} "
                  f"{cs_str}({cs_note}) {mode_tag}")
            if conflict:
                print(f"     ⚡ {conflict}")
            if r['signals']:
                for s in r['signals']:
                    print(f"     → {s}")
            print(f"     判定: {vmap[r['final']]:<4} 置信: {r['confidence']}")
    
    # ═══ 风险汇总 ═══
    print(f"\n{'='*100}")
    print(f"🎯 关键风险点")
    print(f"{'='*100}")
    
    conflicts = [r for r in results if r['cs']>0 and r['cs']<5 and r['verdict']=='D']
    if conflicts:
        print(f"\n⚡ D-Gate判平局但cs锁定屠杀 ({len(conflicts)}场):")
        for r in conflicts:
            print(f"  {r['home']} vs {r['away']} ({r['date']}): D-Gate[{r['mode']}] cs={r['cs']:.0f} → 建议改判屠杀")
    
    high_conf_draws = [r for r in results if r['verdict']=='D' and r['cs']>15]
    if high_conf_draws:
        print(f"\n✅ 高置信平局 (D-Gate+cs双确认) ({len(high_conf_draws)}场):")
        for r in high_conf_draws:
            print(f"  {r['home']} vs {r['away']} ({r['date']}): mode={r['mode']} cs={r['cs']:.0f}")
    
    upsets = [r for r in results if r['elo_home']>0.55 and r['imp_h']<0.40 and r['cs']>20]
    if upsets:
        print(f"\n💡 Elo实力强但赔率低估 ({len(upsets)}场):")
        for r in upsets:
            print(f"  {r['home']} vs {r['away']} ({r['date']}): Elo={r['elo_home']:.0%} 赔率={r['imp_h']:.0%} cs={r['cs']:.0f}")

    return results

if __name__ == "__main__":
    main()
