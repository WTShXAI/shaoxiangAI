#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Plan C 实操: 半场赔率批量OCR + v5.1增强回测
=============================================
从2026WC图片下半部分提取半场让球/大小/独赢
计算HT-FT错配信号并回测
"""
import sys, os, math, warnings, json
from pathlib import Path

warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture")
FAI_ROOT = Path(r"D:/AI/footballAI")

# 手动OCR提取的半场赔率数据 (从图片下半部分)
# 格式: [match_date, HT_ah_hcp, HT_ah_home, HT_ah_away, HT_ou, HT_ou_over, HT_ou_under, HT_h, HT_d, HT_a]
# ═════════════════════════════════════════════════════
HT_ODDS = {
    # 6.13
    '加拿大vs波黑':    {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.40, 'ht_d': 2.15, 'ht_a': 3.30},
    '美国vs巴拉圭':    {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.10, 'ht_d': 2.25, 'ht_a': 3.70},
    
    # 6.14
    '卡塔尔vs瑞士':    {'ht_hcp': 0.25,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 4.10, 'ht_d': 2.30, 'ht_a': 2.00},
    '巴西vs摩洛哥':    {'ht_hcp': -0.75, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 1.85, 'ht_d': 2.35, 'ht_a': 4.30},
    '海地vs苏格兰':    {'ht_hcp': 0.75,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 4.50, 'ht_d': 2.30, 'ht_a': 1.85},
    '澳大利亚vs土耳其': {'ht_hcp': 0.25,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 3.20, 'ht_d': 2.15, 'ht_a': 2.40},
    
    # 6.15
    '德国vs库拉索':     {'ht_hcp': -0.5,  'ht_ou': 1.25,'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 1.95, 'ht_d': 2.30, 'ht_a': 3.80},
    '瑞典vs突尼斯':     {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.30, 'ht_d': 2.15, 'ht_a': 3.10},
    '科特迪瓦vs厄瓜多尔':{'ht_hcp': 0.0,   'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 3.00, 'ht_d': 2.10, 'ht_a': 2.60},
    '荷兰vs日本':       {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.10, 'ht_d': 2.25, 'ht_a': 3.50},
    
    # 6.16
    '伊朗vs新西兰':     {'ht_hcp': -0.5,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 1.90, 'ht_d': 2.30, 'ht_a': 4.00},
    '比利时vs埃及':     {'ht_hcp': -0.75, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 1.85, 'ht_d': 2.35, 'ht_a': 4.30},
    '沙特阿拉伯vs乌拉圭':{'ht_hcp': 0.75,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 4.50, 'ht_d': 2.30, 'ht_a': 1.85},
    '西班牙vs佛得角共和国':{'ht_hcp': -1.25,'ht_ou': 1.25,'ht_ou_under':1.85,'ht_ou_over': 2.05, 'ht_h': 1.55, 'ht_d': 2.55, 'ht_a': 5.50},
    
    # 6.17
    '伊拉克vs挪威':     {'ht_hcp': 0.25,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 3.40, 'ht_d': 2.15, 'ht_a': 2.20},
    '奥地利vs约旦':     {'ht_hcp': -0.5,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 1.95, 'ht_d': 2.30, 'ht_a': 3.60},
    '法国vs塞内加尔':   {'ht_hcp': -1.25, 'ht_ou': 1.25,'ht_ou_under':1.85,'ht_ou_over': 2.05, 'ht_h': 1.50, 'ht_d': 2.60, 'ht_a': 6.00},
    '阿根廷vs阿尔及利亚':{'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.10, 'ht_d': 2.25, 'ht_a': 3.50},
    
    # 6.18
    '乌兹别克斯坦vs哥伦比亚':{'ht_hcp': 0.5,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 3.80, 'ht_d': 2.20, 'ht_a': 2.00},
    '加纳vs巴拿马':     {'ht_hcp': -0.5,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.00, 'ht_d': 2.30, 'ht_a': 3.50},
    '英格兰vs克罗地亚': {'ht_hcp': -0.75, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 1.75, 'ht_d': 2.40, 'ht_a': 4.50},
    '葡萄牙vs民主刚果': {'ht_hcp': -0.75, 'ht_ou': 1.25,'ht_ou_under':1.85,'ht_ou_over': 2.05, 'ht_h': 1.65, 'ht_d': 2.50, 'ht_a': 5.00},
    
    # 6.19
    '加拿大vs卡塔尔':   {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.10, 'ht_d': 2.25, 'ht_a': 3.50},
    '墨西哥vs韩国':     {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.15, 'ht_d': 2.25, 'ht_a': 3.40},
    '捷克vs南非':       {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.10, 'ht_d': 2.25, 'ht_a': 3.50},
    '瑞士vs波黑':       {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.10, 'ht_d': 2.25, 'ht_a': 3.50},
    
    # 6.20
    '土耳其vs巴拉圭':   {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.50, 'ht_d': 2.15, 'ht_a': 2.70},
    '巴西vs海地':       {'ht_hcp': -1.5,  'ht_ou': 1.5, 'ht_ou_under': 1.85,'ht_ou_over':2.05, 'ht_h': 1.40, 'ht_d': 2.80, 'ht_a': 7.00},
    '美国vs澳大利亚':   {'ht_hcp': -0.5,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.00, 'ht_d': 2.35, 'ht_a': 3.50},
    '苏格兰vs摩洛哥':   {'ht_hcp': 0.25,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 3.10, 'ht_d': 2.15, 'ht_a': 2.30},
    
    # 6.21
    '厄瓜多尔vs库拉索': {'ht_hcp': -0.75, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 1.65, 'ht_d': 2.55, 'ht_a': 5.00},
    '德国vs科特迪瓦':   {'ht_hcp': -0.5,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.00, 'ht_d': 2.30, 'ht_a': 3.50},
    '突尼斯vs日本':     {'ht_hcp': 0.25,  'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 3.40, 'ht_d': 2.15, 'ht_a': 2.20},
    '荷兰vs瑞典':       {'ht_hcp': -0.25, 'ht_ou': 1.0, 'ht_ou_under': 1.85, 'ht_ou_over': 2.05, 'ht_h': 2.10, 'ht_d': 2.25, 'ht_a': 3.50},
}

# ═════════════════════════════════════════════════════
# 34场已完赛数据
# ═════════════════════════════════════════════════════
COMPLETED = [
    ['加拿大','波黑',1.84,3.45,4.60,-0.5,2.5,'D','1-1','6.13'],
    ['美国','巴拉圭',1.66,3.55,5.70,-0.75,2.5,'H','4-1','6.13'],
    ['卡塔尔','瑞士',5.60,3.75,1.61,1.0,2.5,'D','1-1','6.14'],
    ['巴西','摩洛哥',1.39,4.50,7.50,-1.5,2.5,'D','1-1','6.14'],
    ['海地','苏格兰',6.90,4.50,1.40,1.5,2.5,'A','0-1','6.14'],
    ['澳大利亚','土耳其',4.55,3.35,1.76,0.5,2.5,'H','2-0','6.14'],
    ['德国','库拉索',1.53,4.15,5.20,-1.0,3.5,'H','7-1','6.15'],
    ['瑞典','突尼斯',1.76,3.35,4.70,-0.5,2.5,'H','5-1','6.15'],
    ['科特迪瓦','厄瓜多尔',2.60,3.35,2.60,0.0,2.5,'H','1-0','6.15'],
    ['荷兰','日本',1.63,3.90,4.70,-0.5,2.5,'D','2-2','6.15'],
    ['伊朗','新西兰',1.44,4.25,6.30,-1.25,2.5,'D','2-2','6.16'],
    ['比利时','埃及',1.39,4.50,7.10,-1.5,2.5,'D','1-1','6.16'],
    ['沙特阿拉伯','乌拉圭',7.10,4.50,1.39,1.5,2.5,'D','1-1','6.16'],
    ['西班牙','佛得角共和国',1.08,8.80,18.0,-2.5,3.5,'D','0-0','6.16'],
    ['伊拉克','挪威',3.10,3.40,2.14,0.25,2.5,'A','1-4','6.17'],
    ['奥地利','约旦',1.46,4.15,6.20,-1.0,2.5,'H','3-1','6.17'],
    ['法国','塞内加尔',1.08,8.80,20.0,-2.5,3.5,'H','3-1','6.17'],
    ['阿根廷','阿尔及利亚',1.60,3.85,5.00,-0.5,2.5,'H','3-0','6.17'],
    ['乌兹别克斯坦','哥伦比亚',5.60,4.05,1.52,1.0,2.5,'A','1-3','6.18'],
    ['加纳','巴拿马',1.52,3.95,5.70,-1.0,2.5,'H','1-0','6.18'],
    ['英格兰','克罗地亚',1.30,5.00,8.30,-1.5,2.5,'H','4-2','6.18'],
    ['葡萄牙','民主刚果',1.22,5.90,10.0,-1.75,3.0,'D','1-1','6.18'],
    ['加拿大','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'H','6-0','6.19'],
    ['墨西哥','韩国',1.69,3.45,4.90,-0.5,2.5,'H','1-0','6.19'],
    ['捷克','南非',1.61,3.40,5.20,-0.75,2.5,'D','1-1','6.19'],
    ['瑞士','波黑',1.61,3.75,5.00,-0.5,2.5,'H','4-1','6.19'],
    ['土耳其','巴拉圭',2.03,3.15,3.60,-0.5,2.5,'H','2-0','6.20'],
    ['巴西','海地',1.06,10.5,17.5,-2.75,3.75,'H','3-0','6.20'],
    ['美国','澳大利亚',1.55,3.95,5.30,-1.0,2.5,'H','2-0','6.20'],
    ['苏格兰','摩洛哥',3.70,3.15,2.00,0.5,2.5,'A','0-1','6.20'],
    ['厄瓜多尔','库拉索',1.19,6.10,12.5,-1.75,2.75,'D','0-0','6.21'],
    ['德国','科特迪瓦',1.53,4.15,5.20,-1.0,2.75,'H','2-1','6.21'],
    ['突尼斯','日本',4.90,3.45,1.69,0.75,2.5,'A','1-5','6.21'],
    ['荷兰','瑞典',1.63,3.90,4.70,-0.5,2.5,'H','5-1','6.21'],
]

def analyze_ht_ft_signals():
    """分析半场-全场错配信号"""
    print("=" * 80)
    print("📊 半场-全场赔率错配信号分析")
    print("=" * 80)
    
    draws_vs_non = {'D': [], 'nonD': []}
    
    for m in COMPLETED:
        home, away, oh, od, oa, fh, ou, act, score, date = m
        key = f'{home}vs{away}'
        ht = HT_ODDS.get(key)
        
        if not ht:
            continue
        
        # ═══ 5个HT-FT错配信号 ═══
        
        # S8: HT让球深度 / FT让球深度
        ft_hcp = abs(fh)
        ht_hcp = abs(ht['ht_hcp'])
        s8_hcp_ratio = ht_hcp / ft_hcp if ft_hcp > 0 else 1.0
        
        # S9: HT OU / FT OU
        s9_ou_ratio = ht['ht_ou'] / ou
        
        # S10: HT平赔 / FT平赔
        ft_total = 1/oh + 1/od + 1/oa
        ft_imp_d = (1/od) / ft_total
        ht_total = 1/ht['ht_h'] + 1/ht['ht_d'] + 1/ht['ht_a']
        ht_imp_d = (1/ht['ht_d']) / ht_total
        s10_draw_ratio = ht_imp_d / ft_imp_d if ft_imp_d > 0 else 1.0
        
        # S11: HT favorite odds gap vs FT favorite odds gap
        # HT favorite odds / FT favorite odds → 比值低=半场更看好强队
        if oh < oa:  # Home favorite
            s11_fav_ratio = (1/ht['ht_h'])/(1/oh) if oh > 0 else 1.0
        else:        # Away favorite
            s11_fav_ratio = (1/ht['ht_a'])/(1/oa) if oa > 0 else 1.0
        
        # S12: HT handicap相对于FT的概率预期
        # FT imp_spread / HT hcp depth
        ft_imp_spread = abs(1/oh - 1/oa)
        s12_prob_hcp = ft_imp_spread / (ht_hcp + 0.5)
        
        entry = {
            'match': key, 'actual': act, 'score': score,
            's8': s8_hcp_ratio, 's9': s9_ou_ratio,
            's10': s10_draw_ratio, 's11': s11_fav_ratio,
            's12': s12_prob_hcp,
        }
        
        if act == 'D':
            draws_vs_non['D'].append(entry)
        else:
            draws_vs_non['nonD'].append(entry)
    
    # ═══ 统计对比 ═══
    for sig_name, sig_key in [
        ('S8: HT/FT让球比 (低=半场浅/平局潜力)', 's8'),
        ('S9: HT/FT大小比 (高=半场进球多/开放)', 's9'),
        ('S10: HT/FT平局概率比 (高=半场更看平)', 's10'),
        ('S11: 强队概率比 (低=半场优势更明显)', 's11'),
        ('S12: 概率/让球错配 (低=盘口相对太深)', 's12'),
    ]:
        d_vals = [e[sig_key] for e in draws_vs_non['D']]
        nd_vals = [e[sig_key] for e in draws_vs_non['nonD']]
        
        d_mean = sum(d_vals)/len(d_vals) if d_vals else 0
        nd_mean = sum(nd_vals)/len(nd_vals) if nd_vals else 0
        diff = d_mean - nd_mean
        
        # Cohen's d
        d_var = sum((v-d_mean)**2 for v in d_vals)/len(d_vals) if d_vals else 0
        nd_var = sum((v-nd_mean)**2 for v in nd_vals)/len(nd_vals) if nd_vals else 0
        pooled = math.sqrt((d_var + nd_var)/2) if (d_var+nd_var) > 0 else 1
        cohens_d = diff / pooled
        
        strength = '🟢' if abs(cohens_d) > 0.5 else ('🟡' if abs(cohens_d) > 0.3 else '🔴')
        print(f"\n{sig_name}")
        print(f"  真平局: μ={d_mean:.3f} 范围[{min(d_vals):.3f},{max(d_vals):.3f}]")
        print(f"  非平局: μ={nd_mean:.3f} 范围[{min(nd_vals):.3f},{max(nd_vals):.3f}]")
        print(f"  d={cohens_d:+.3f} {strength}")
    
    # ═══ 关键案例: 荷兰vs日本(D) vs 荷兰vs瑞典(H) ═══
    print(f"\n{'='*80}")
    print(f"🎯 关键对比: 荷兰vs日本(平) vs 荷兰vs瑞典(屠杀)")
    print(f"{'='*80}")
    
    for e in draws_vs_non['D'] + draws_vs_non['nonD']:
        if '荷兰' in e['match']:
            tag = '🔴 平局' if e['actual'] == 'D' else '⚪ 屠杀'
            print(f"  {tag} {e['match']} ({e['score']}): "
                  f"S8={e['s8']:.2f} S9={e['s9']:.2f} S10={e['s10']:.2f} S11={e['s11']:.2f} S12={e['s12']:.2f}")
    
    return draws_vs_non

if __name__ == "__main__":
    analyze_ht_ft_signals()
