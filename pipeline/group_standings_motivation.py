#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WC2026 小组积分 + 出线形势 + 战意分析
==========================================
基于34场已赛结果重建12组积分榜
分析剩余36场的出线形势, 标注死战/走过场/荣誉战
与D-Gate v5.1预测联动输出最终建议
"""
import sys, os, math, warnings
from pathlib import Path
from collections import defaultdict
warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture v4.0")
FAI_ROOT = Path(r"D:/AI/footballAI")
sys.path.insert(0, str(ARCH_ROOT))
sys.path.insert(0, str(ARCH_ROOT / "features"))
sys.path.insert(0, str(ARCH_ROOT / "predictors"))
sys.path.insert(0, str(ARCH_ROOT / "rules"))

# ═══════════════════════════════════════
# 12组定义
# ═══════════════════════════════════════
GROUPS = {
    'A': ['加拿大','波黑','卡塔尔','瑞士'],
    'B': ['美国','巴拉圭','澳大利亚','土耳其'],
    'C': ['巴西','摩洛哥','海地','苏格兰'],
    'D': ['德国','库拉索','科特迪瓦','厄瓜多尔'],
    'E': ['荷兰','日本','瑞典','突尼斯'],
    'F': ['伊朗','新西兰','比利时','埃及'],
    'G': ['西班牙','佛得角共和国','沙特阿拉伯','乌拉圭'],
    'H': ['法国','塞内加尔','伊拉克','挪威'],
    'I': ['阿根廷','阿尔及利亚','奥地利','约旦'],
    'J': ['英格兰','克罗地亚','加纳','巴拿马'],
    'K': ['葡萄牙','民主刚果','哥伦比亚','乌兹别克斯坦'],
    'L': ['墨西哥','韩国','捷克','南非'],
}

# ═══════════════════════════════════════
# 34场已完成比赛 (含比分)
# ═══════════════════════════════════════
COMPLETED = [
    # (主队, 客队, 主进球, 客进球, 日期)
    ('加拿大','波黑',1,1,'6.13'), ('美国','巴拉圭',4,1,'6.13'),
    ('卡塔尔','瑞士',1,1,'6.14'), ('巴西','摩洛哥',1,1,'6.14'),
    ('海地','苏格兰',0,1,'6.14'), ('澳大利亚','土耳其',2,0,'6.14'),
    ('德国','库拉索',7,1,'6.15'), ('瑞典','突尼斯',5,1,'6.15'),
    ('科特迪瓦','厄瓜多尔',1,0,'6.15'), ('荷兰','日本',2,2,'6.15'),
    ('伊朗','新西兰',2,2,'6.16'), ('比利时','埃及',1,1,'6.16'),
    ('沙特阿拉伯','乌拉圭',1,1,'6.16'), ('西班牙','佛得角共和国',0,0,'6.16'),
    ('伊拉克','挪威',1,4,'6.17'), ('奥地利','约旦',3,1,'6.17'),
    ('法国','塞内加尔',3,1,'6.17'), ('阿根廷','阿尔及利亚',3,0,'6.17'),
    ('乌兹别克斯坦','哥伦比亚',1,3,'6.18'), ('加纳','巴拿马',1,0,'6.18'),
    ('英格兰','克罗地亚',4,2,'6.18'), ('葡萄牙','民主刚果',1,1,'6.18'),
    ('加拿大','卡塔尔',6,0,'6.19'), ('墨西哥','韩国',1,0,'6.19'),
    ('捷克','南非',1,1,'6.19'), ('瑞士','波黑',4,1,'6.19'),
    ('土耳其','巴拉圭',2,0,'6.20'), ('巴西','海地',3,0,'6.20'),
    ('美国','澳大利亚',2,0,'6.20'), ('苏格兰','摩洛哥',0,1,'6.20'),
    ('厄瓜多尔','库拉索',0,0,'6.21'), ('德国','科特迪瓦',2,1,'6.21'),
    ('突尼斯','日本',1,5,'6.21'), ('荷兰','瑞典',5,1,'6.21'),
]

# ═══════════════════════════════════════
# 36场未赛 (含赔率)
# ═══════════════════════════════════════
FUTURE = [
    # date, home, away, oh, od, oa, hcp, ou, cs_other
    ['6.22','乌拉圭','佛得角共和国',1.44,4.25,6.30,-1.25,2.5,23],
    ['6.22','新西兰','埃及',4.55,3.35,1.76,0.75,2.5,24],
    ['6.22','比利时','伊朗',1.39,4.50,7.10,-1.5,2.5,14],
    ['6.22','西班牙','沙特阿拉伯',1.08,8.80,18.0,-2.5,3.5,4],
    ['6.23','挪威','塞内加尔',2.14,3.40,3.10,0.0,2.5,33],
    ['6.23','法国','伊拉克',1.08,8.80,20.0,-2.5,3.5,4],
    ['6.23','约旦','阿尔及利亚',6.20,4.15,1.46,1.0,2.5,21],
    ['6.23','阿根廷','奥地利',1.60,3.85,5.00,-0.5,2.5,20],
    ['6.24','哥伦比亚','民主刚果',1.44,4.05,6.90,-1.0,2.5,26],
    ['6.24','巴拿马','克罗地亚',5.70,3.95,1.52,1.0,2.5,19],
    ['6.24','英格兰','加纳',1.30,5.00,8.30,-1.5,2.5,8],
    ['6.24','葡萄牙','乌兹别克斯坦',1.22,5.90,10.0,-1.75,3.0,8],
    ['6.25','南非','韩国',4.95,3.80,1.61,0.75,2.5,25],
    ['6.25','捷克','墨西哥',4.25,3.35,1.81,-0.5,2.5,37],
    ['6.25','摩洛哥','海地',1.34,4.85,7.50,-1.5,2.75,7],
    ['6.25','波黑','卡塔尔',1.61,3.75,5.00,-0.5,2.5,12],
    ['6.25','瑞士','加拿大',2.12,3.25,3.25,-0.25,2.25,45],
    ['6.25','苏格兰','巴西',6.90,4.50,1.40,1.5,2.5,14],
    ['6.26','厄瓜多尔','德国',4.45,3.55,1.72,-0.75,2.5,25],
    ['6.26','土耳其','美国',2.60,3.50,2.41,0.0,2.5,24],
    ['6.26','巴拉圭','澳大利亚',2.09,3.20,3.40,-0.25,2.25,94],
    ['6.26','库拉索','科特迪瓦',11.0,5.80,1.21,-1.75,2.75,6],
    ['6.26','日本','瑞典',2.11,3.30,3.25,-0.25,2.5,27],
    ['6.26','突尼斯','荷兰',5.80,4.15,1.49,-1.0,2.5,6],
    ['6.27','乌拉圭','西班牙',4.70,3.90,1.63,-0.75,2.5,20],
    ['6.27','佛得角共和国','沙特阿拉伯',2.47,3.35,2.62,0.0,2.25,49],
    ['6.27','埃及','伊朗',2.16,3.00,3.40,-0.25,2.0,53],
    ['6.27','塞内加尔','伊拉克',1.40,4.40,7.00,-1.25,2.5,12],
    ['6.27','挪威','法国',4.05,3.55,1.80,-0.75,2.5,22],
    ['6.27','新西兰','比利时',9.00,5.20,1.28,-1.5,2.75,9],
    ['6.28','克罗地亚','加纳',1.62,3.75,5.00,-0.75,2.5,25],
    ['6.28','哥伦比亚','葡萄牙',3.25,3.30,2.11,-0.25,2.25,40],
    ['6.28','巴拿马','英格兰',9.10,5.40,1.27,-1.5,2.75,8],
    ['6.28','约旦','阿根廷',12.0,6.30,1.18,-2.0,3.0,8],
    ['6.28','阿尔及利亚','奥地利',3.30,3.25,2.11,-0.25,2.25,47],
    ['6.28','民主刚果','乌兹别克斯坦',2.27,3.25,2.97,0.0,2.25,42],
]

# ═══════════════════════════════════════
# 1. 计算积分榜
# ═══════════════════════════════════════
def compute_standings():
    """为每组计算积分/PTS/GF/GA/GD"""
    standings = {g: {} for g in GROUPS}
    for g, teams in GROUPS.items():
        for t in teams:
            standings[g][t] = {'P':0, 'W':0, 'D':0, 'L':0, 'GF':0, 'GA':0, 'GD':0, 'PTS':0}
    
    for h, a, hg, ag, _date in COMPLETED:
        # 找到组
        group = None
        for g, teams in GROUPS.items():
            if h in teams and a in teams:
                group = g
                break
        if not group:
            continue
        
        standings[group][h]['P'] += 1
        standings[group][a]['P'] += 1
        standings[group][h]['GF'] += hg
        standings[group][h]['GA'] += ag
        standings[group][a]['GF'] += ag
        standings[group][a]['GA'] += hg
        
        if hg > ag:
            standings[group][h]['W'] += 1
            standings[group][h]['PTS'] += 3
            standings[group][a]['L'] += 1
        elif ag > hg:
            standings[group][a]['W'] += 1
            standings[group][a]['PTS'] += 3
            standings[group][h]['L'] += 1
        else:
            standings[group][h]['D'] += 1
            standings[group][a]['D'] += 1
            standings[group][h]['PTS'] += 1
            standings[group][a]['PTS'] += 1
    
    # 计算GD
    for g in standings:
        for t in standings[g]:
            s = standings[g][t]
            s['GD'] = s['GF'] - s['GA']
    
    return standings

# ═══════════════════════════════════════
# 2. 出线形势分析
# ═══════════════════════════════════════
def analyze_qualification(standings):
    """
    分析每组的出线形势
    返回每场比赛的战意标签
    """
    group_status = {}
    
    for g, teams in GROUPS.items():
        tbl = [(t, standings[g][t]) for t in teams]
        tbl.sort(key=lambda x: (-x[1]['PTS'], -x[1]['GD'], -x[1]['GF']))
        
        max_pts_possible = {}
        for t in teams:
            remaining = 3 - standings[g][t]['P']
            max_pts_possible[t] = standings[g][t]['PTS'] + remaining * 3
        
        # 确定已晋级/已淘汰
        status = {}
        for t in teams:
            s = standings[g][t]
            status[t] = {
                'rank': [i+1 for i, (tn, _) in enumerate(tbl) if tn == t][0],
                'pts': s['PTS'], 'gd': s['GD'], 'gf': s['GF'],
                'played': s['P'], 'remaining': 3 - s['P'],
                'max_pts': max_pts_possible[t],
            }
        
        # 判定出线状态
        for t in teams:
            s = status[t]
            # 已确保前二: 第三名即使全胜也无法超越
            if s['rank'] <= 2 and s['remaining'] == 0:
                s['status'] = '🔒已晋级(赛程结束)'
                s['motivation'] = 'rotation'  # 可能轮换
            elif s['rank'] <= 2 and s['pts'] - tbl[2][1]['PTS'] > 3 * s['remaining']:
                s['status'] = '🔒锁定晋级'
                s['motivation'] = 'rotation'
            # 已淘汰: 即使全胜也追不上第二名
            elif s['rank'] >= 3:
                second_pts = tbl[1][1]['PTS']
                second_remaining = 3 - tbl[1][1]['P']
                if s['max_pts'] < second_pts:
                    s['status'] = '❌已淘汰'
                    s['motivation'] = 'dead'
                elif s['max_pts'] == second_pts and s['gd'] < tbl[1][1]['GD'] - 10:
                    s['status'] = '❌基本淘汰(GD劣势)'
                    s['motivation'] = 'dead'
                elif s['remaining'] == 0:
                    s['status'] = '❌已淘汰'
                    s['motivation'] = 'dead'
                else:
                    s['status'] = '⚠️需追赶'
                    s['motivation'] = 'must_win'
            # 还有比赛
            elif s['remaining'] > 0:
                if s['rank'] == 1:
                    # 领先第三名优势
                    third_pts = tbl[2][1]['PTS']
                    if s['pts'] - third_pts >= 3:
                        s['status'] = '✅基本晋级'
                        s['motivation'] = 'comfortable'
                    else:
                        s['status'] = '⚠️未锁定'
                        s['motivation'] = 'must_win'
                elif s['rank'] == 2:
                    third_pts = tbl[2][1]['PTS']
                    if s['pts'] - third_pts >= 3:
                        s['status'] = '✅基本晋级'
                        s['motivation'] = 'comfortable'
                    elif s['pts'] > third_pts:
                        s['status'] = '⚠️保二争一'
                        s['motivation'] = 'must_win'
                    else:
                        s['status'] = '🔥生死战'
                        s['motivation'] = 'must_win'
                
                # 淘汰区边缘
                if s['max_pts'] < tbl[1][1]['PTS']:
                    s['status'] = '❌最多第三'
                    s['motivation'] = 'dead'
            else:
                s['status'] = '已完赛'
                s['motivation'] = 'done'
        
        # 特殊检查: 如果第4名还剩0场且被淘汰
        for i, (t, _) in enumerate(tbl):
            if status[t]['remaining'] == 0 and status[t]['pts'] < tbl[1][1]['PTS']:
                if '已淘汰' not in status[t].get('status',''):
                    status[t]['status'] = '❌已淘汰(积分不够)'
                    status[t]['motivation'] = 'dead'
        
        group_status[g] = status
    
    return group_status

# ═══════════════════════════════════════
# 3. D-Gate v5.1 判型
# ═══════════════════════════════════════
def dgate_v51(ph, pd, pa, oh, od, oa, hcp, ou):
    spread = abs(ph-pa)
    max_imp = max(ph, pa)
    s1 = od/math.sqrt(oh*oa)
    s7 = ou/max(abs(hcp),0.25)
    
    if max_imp >= 0.70:
        d = pd*1.08
        d *= 2.2 if (max_imp>0.75 or abs(hcp)>=1.75) else 1.8
        if od>9.5 and ou>=3.5 and abs(hcp)>=2.5: d*=0.3
        elif od>9.5 and abs(hcp)>=2.5: d*=0.5
        if d>0.14: return 'D', 'C', d, s1, s7
    
    if pa>0.65 and max_imp<0.70:
        d = pd*1.08*2.0
        if d>0.14: return 'D', 'C-away', d, s1, s7
    
    if 0.48<=max_imp<=0.70:
        d = pd*1.08
        d *= max(0.80, 1-spread*0.30)
        if ou<=2.5: d*=1.05
        if s7>=3.5 and s1<1.30: d*=0.70
        if d>0.28: return 'D', 'A', d, s1, s7
    
    if spread<0.15:
        d = pd*1.08*1.20
        if d>0.43: return 'D', 'B', d, s1, s7
    
    d = pd*1.08
    if spread>0.40: d*=0.70
    elif spread>0.20: d*=0.85
    if s7>=3.5 and s1<1.30: d*=0.70
    if d>0.32: return 'D', 'default', d, s1, s7
    
    return ('H' if ph>pa else 'A'), 'normal', d, s1, s7

# ═══════════════════════════════════════
# 4. 战意调整
# ═══════════════════════════════════════
MOTIVATION_MULTIPLIER = {
    'must_win': {'H_boost': 1.08, 'A_boost': 1.08, 'draw_penalty': 0.90, 'note': '必须赢'},
    'comfortable': {'H_boost': 1.02, 'A_boost': 1.02, 'draw_penalty': 0.95, 'note': '稳妥即可'},
    'dead': {'H_boost': 0.90, 'A_boost': 0.90, 'draw_penalty': 1.00, 'note': '已淘汰/轮换'},
    'rotation': {'H_boost': 0.93, 'A_boost': 0.93, 'draw_penalty': 0.97, 'note': '锁定后轮换'},
    'done': {'H_boost': 1.00, 'A_boost': 1.00, 'draw_penalty': 1.00, 'note': '-'},
}

def adjust_for_motivation(home, away, h_motiv, a_motiv, base_verdict, base_d_boost, imp_h, imp_a):
    """根据战意调整预测"""
    hm = MOTIVATION_MULTIPLIER.get(h_motiv, MOTIVATION_MULTIPLIER['done'])
    am = MOTIVATION_MULTIPLIER.get(a_motiv, MOTIVATION_MULTIPLIER['done'])
    
    # 同级别 → 无调整
    if h_motiv == a_motiv:
        return base_verdict, 0, '同级'
    
    adjustments = []
    
    # 主队必须赢 vs 客队已淘汰 → 主队大优
    if h_motiv == 'must_win' and a_motiv in ('dead', 'rotation'):
        adjustments.append(('H强势', 1.06))
    elif h_motiv in ('dead', 'rotation') and a_motiv == 'must_win':
        adjustments.append(('A强势', 0.94))
    
    # 主队必须赢 vs 客队舒适
    elif h_motiv == 'must_win' and a_motiv == 'comfortable':
        adjustments.append(('H略优', 1.03))
    elif h_motiv == 'comfortable' and a_motiv == 'must_win':
        adjustments.append(('A略优', 0.97))
    
    # 双方都淘汰 → 荣誉战, 可能大开大合, 平局减少
    if h_motiv == 'dead' and a_motiv == 'dead':
        adjustments.append(('荣誉战', 1.00))
    
    # 一方锁定一方必须赢 → 锁定方可能松懈
    if h_motiv == 'rotation' and a_motiv == 'must_win':
        adjustments.append(('A趁虚', 0.90))
    elif h_motiv == 'must_win' and a_motiv == 'rotation':
        adjustments.append(('H趁虚', 1.10))
    
    if not adjustments:
        return base_verdict, 0, '无显著差异'
    
    # 综合调整
    total_adj = 1.0
    for label, factor in adjustments:
        total_adj *= factor
    
    # 如果D-Gate已经判平, 战意差异会削弱平局概率
    if base_verdict == 'D':
        if 'must_win' in (h_motiv, a_motiv) and 'dead' not in (h_motiv, a_motiv):
            # 双方都有战意 + D-Gate判平 → 置信降低
            return 'D', total_adj - 1.0, f'战意削弱({";".join(a[0] for a in adjustments)})'
    
    # 如果双方都淘汰, 增加冷门可能
    if h_motiv == 'dead' and a_motiv == 'dead' and base_verdict != 'D':
        return base_verdict, 0, '荣誉战(无调整)'
    
    note = ';'.join(a[0] for a in adjustments)
    return base_verdict, total_adj - 1.0, note

# ═══════════════════════════════════════
# 5. 综合预测
# ═══════════════════════════════════════
def main():
    standings = compute_standings()
    group_status = analyze_qualification(standings)
    
    # ═══ 打印积分榜 ═══
    print("=" * 110)
    print("⚽ WC2026 小组积分榜 — 34场已赛后")
    print("=" * 110)
    
    for g in ['A','B','C','D','E','F','G','H','I','J','K','L']:
        teams = GROUPS[g]
        tbl = [(t, standings[g][t], group_status[g][t]) for t in teams]
        tbl.sort(key=lambda x: (-x[1]['PTS'], -x[1]['GD'], -x[1]['GF']))
        
        remaining_matches = sum(1 for t in teams if group_status[g][t]['remaining'] > 0)
        print(f"\n{'─'*110}")
        print(f"🏆 Group {g}: {' | '.join(teams)}          [剩余{remaining_matches}场]")
        print(f"{'─'*110}")
        print(f"  {'排名':<4} {'球队':<16} {'赛':>2} {'胜':>2} {'平':>2} {'负':>2} {'GF':>3} {'GA':>3} {'GD':>4} {'PTS':>3} {'出线形势':<25}")
        print(f"  {'─'*100}")
        
        for i, (t, s, st) in enumerate(tbl):
            rank_icon = ['🥇','🥈','🥉','4️⃣'][i]
            print(f"  {rank_icon:<4} {t:<16} {s['P']:>2} {s['W']:>2} {s['D']:>2} {s['L']:>2} "
                  f"{s['GF']:>3} {s['GA']:>3} {s['GD']:>4} {s['PTS']:>3} {st['status']:<25}")
    
    # ═══ 逐场预测 ═══
    print(f"\n\n{'='*110}")
    print(f"🎯 36场预测 — D-Gate v5.1 + 战意联动")
    print(f"{'='*110}")
    
    recommendations = []
    by_date = defaultdict(list)
    
    for m in FUTURE:
        date, home, away, oh, od, oa, hcp, ou, cs = m
        
        # 找组
        group = None
        for g, teams in GROUPS.items():
            if home in teams and away in teams:
                group = g
                break
        
        # 赔率隐含概率
        total = 1/oh + 1/od + 1/oa
        imp_h = (1/oh)/total
        imp_d = (1/od)/total
        imp_a = (1/oa)/total
        
        # D-Gate
        verdict, mode, d_boost, s1, s7 = dgate_v51(imp_h, imp_d, imp_a, oh, od, oa, hcp, ou)
        
        # 战意
        h_st = group_status.get(group, {}).get(home, {})
        a_st = group_status.get(group, {}).get(away, {})
        h_motiv = h_st.get('motivation', 'unknown')
        a_motiv = a_st.get('motivation', 'unknown')
        
        # 战意调整
        final_v, motiv_adj, motiv_note = adjust_for_motivation(
            home, away, h_motiv, a_motiv, verdict, d_boost, imp_h, imp_a
        )
        
        # cs验证
        cs_confirm = ''
        if cs > 0 and cs < 5 and verdict == 'D':
            cs_confirm = '⚡cs否决!'
            if final_v == 'D':
                final_v = 'H' if imp_h > imp_a else 'A'
                motiv_note += ' + cs屠杀信号'
        elif cs > 15 and verdict == 'D':
            cs_confirm = '✅cs确认'
        elif cs > 25:
            cs_confirm = '⚠️极高不确定'
        
        # 综合评定
        signal_tags = []
        if verdict != 'normal' and verdict != ('H' if imp_h > imp_a else 'A'):
            signal_tags.append(f'D-Gate[{mode}]')
        if h_motiv != a_motiv:
            signal_tags.append(f'战意:{motiv_note}')
        if cs_confirm:
            signal_tags.append(cs_confirm)
        
        # 推荐置信
        confidence = 'medium'
        if final_v == verdict and cs > 15:
            confidence = 'high'
        elif 'cs否决' in cs_confirm:
            confidence = 'high'
        elif h_motiv == 'must_win' and a_motiv == 'dead':
            confidence = 'high'
        elif mode in ('C', 'C-away') and cs < 5:
            confidence = 'high'
        
        vmap = {'H': '主胜', 'D': '平局', 'A': '客胜'}
        
        rec = {
            'date': date, 'home': home, 'away': away, 'group': group,
            'imp_h': imp_h, 'imp_d': imp_d, 'imp_a': imp_a,
            'dgate': verdict, 'mode': mode, 'cs': cs,
            'h_motiv': h_motiv, 'a_motiv': a_motiv,
            'final': final_v, 'confidence': confidence,
            'signals': signal_tags,
            'h_status': h_st.get('status','?'),
            'a_status': a_st.get('status','?'),
        }
        recommendations.append(rec)
        by_date[date].append(rec)
    
    # 按日期输出
    for date in sorted(by_date.keys()):
        recs = by_date[date]
        print(f"\n{'─'*110}")
        print(f"📅 {date} ({len(recs)}场)")
        print(f"{'─'*110}")
        
        for r in recs:
            dgate_tag = '🔴D' if r['dgate'] == 'D' else '  '
            conf_icon = {'high': '🟢', 'medium': '🟡', 'low': '🔴'}.get(r['confidence'], '?')
            cs_str = f'cs={r["cs"]:.0f}' if r['cs'] > 0 else 'cs=?'
            
            print(f"  {conf_icon} {dgate_tag} [{r['group']}] {r['home']:<14} vs {r['away']:<16} "
                  f"→ {vmap[r['final']]:<4} 置信:{r['confidence']:<6}")
            print(f"     赔率:H={r['imp_h']:.0%} D={r['imp_d']:.0%} "
                  f"({r['h_status']} vs {r['a_status']}) {cs_str}")
            if r['signals']:
                for s in r['signals']:
                    print(f"     → {s}")
    
    # ═══ 重点推荐 ═══
    print(f"\n\n{'='*110}")
    print(f"🔥 重点推荐 (高置信场次)")
    print(f"{'='*110}")
    
    high_conf = [r for r in recommendations if r['confidence'] == 'high']
    high_conf.sort(key=lambda r: r['date'])
    
    if high_conf:
        for r in high_conf:
            print(f"  🎯 {r['date']} [{r['group']}] {r['home']} vs {r['away']} → {vmap[r['final']]}")
            print(f"     理由: {' | '.join(r['signals'])}")
    else:
        print("  (无高置信场次)")
    
    # ═══ 风险预警 ═══
    print(f"\n{'='*110}")
    print(f"⚠️ 风险预警")
    print(f"{'='*110}")
    
    # 死战场次 (可能出冷门)
    dead_rubbers = [r for r in recommendations 
                    if r['h_motiv'] in ('dead','rotation') and r['a_motiv'] in ('dead','rotation')]
    if dead_rubbers:
        print(f"\n  荣誉战/死战 ({len(dead_rubbers)}场, 谨慎下注):")
        for r in dead_rubbers:
            print(f"  {r['date']} [{r['group']}] {r['home']} vs {r['away']}: "
                  f"{r['h_status']} | {r['a_status']}")
    
    # D-Gate vs cs冲突
    conflicts = [r for r in recommendations if 'cs否决' in ' '.join(r['signals'])]
    if conflicts:
        print(f"\n  D-Gate vs cs冲突 ({len(conflicts)}场):")
        for r in conflicts:
            print(f"  {r['date']} [{r['group']}] {r['home']} vs {r['away']}: "
                  f"D-Gate={r['dgate']}→cs={r['cs']:.0f}否决→判{vmap[r['final']]}")
    
    # 平衡赛 D-Gate警告
    balance_risks = [r for r in recommendations if r['mode'] in ('A','C') and r['dgate'] == 'D']
    if balance_risks:
        print(f"\n  D-Gate平局预警 (Mode A/C, {len(balance_risks)}场, 历史误判率55%):")
        for r in balance_risks:
            print(f"  {r['date']} [{r['group']}] {r['home']} vs {r['away']}: "
                  f"mode={r['mode']} dgate→D ({r['h_status']}|{r['a_status']})")
    
    return standings, group_status, recommendations

if __name__ == "__main__":
    main()
