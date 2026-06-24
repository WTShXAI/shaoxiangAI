#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
v5.1 12个误判 — 按进球数分层深度分析
======================================
假设: 低进球(1-2)误判 = 差点平局, 高进球(3+)误判 = 真屠杀
目标: 找到高进球误判的共性, 设计"屠杀指数"过滤
"""
import sys, os, math, warnings
from pathlib import Path
warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture v4.0")
FAI_ROOT = Path(r"D:/AI/footballAI")
sys.path.insert(0, str(ARCH_ROOT))
sys.path.insert(0, str(ARCH_ROOT / "features"))
sys.path.insert(0, str(ARCH_ROOT / "predictors"))
sys.path.insert(0, str(ARCH_ROOT / "rules"))
sys.path.insert(0, str(FAI_ROOT))

# ═══════════════════════════════════════════════════
# 12个v5.1误判 (来自生产引擎回测)
# ═══════════════════════════════════════════════════
FALSE_POSITIVES = [
    # match, date, oh, od, oa, hcp, ou, actual, score, dg_mode
    ['美国','巴拉圭',1.66,3.55,5.70,-0.75,2.5,'H','4-1','A'],
    ['海地','苏格兰',6.90,4.50,1.40,1.5,2.5,'A','0-1','C-away'],
    ['伊拉克','挪威',3.10,3.40,2.14,0.25,2.5,'A','1-4','default'],
    ['奥地利','约旦',1.46,4.15,6.20,-1.0,2.5,'H','3-1','C'],
    ['法国','塞内加尔',1.08,8.80,20.0,-2.5,3.5,'H','3-1','C'],
    ['阿根廷','阿尔及利亚',1.60,3.85,5.00,-0.5,2.5,'H','3-0','A'],
    ['乌兹别克斯坦','哥伦比亚',5.60,4.05,1.52,1.0,2.5,'A','1-3','A'],
    ['英格兰','克罗地亚',1.30,5.00,8.30,-1.5,2.5,'H','4-2','C'],
    ['加拿大','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'H','6-0','A'],
    ['瑞士','波黑',1.61,3.75,5.00,-0.5,2.5,'H','4-1','A'],
    ['突尼斯','日本',4.90,3.45,1.69,0.75,2.5,'A','1-5','A'],
    ['荷兰','瑞典',1.63,3.90,4.70,-0.5,2.5,'H','5-1','A'],
]

# 真正的10场平局 (对比组)
TRUE_DRAWS = [
    ['加拿大','波黑',1.84,3.45,4.60,-0.5,2.5,'D','1-1'],
    ['卡塔尔','瑞士',5.60,3.75,1.61,1.0,2.5,'D','1-1'],
    ['巴西','摩洛哥',1.39,4.50,7.50,-1.5,2.5,'D','1-1'],
    ['荷兰','日本',1.63,3.90,4.70,-0.5,2.5,'D','2-2'],
    ['伊朗','新西兰',1.44,4.25,6.30,-1.25,2.5,'D','2-2'],
    ['比利时','埃及',1.39,4.50,7.10,-1.5,2.5,'D','1-1'],
    ['沙特阿拉伯','乌拉圭',7.10,4.50,1.39,1.5,2.5,'D','1-1'],
    ['西班牙','佛得角共和国',1.08,8.80,18.0,-2.5,3.5,'D','0-0'],
    ['葡萄牙','民主刚果',1.22,5.90,10.0,-1.75,3.0,'D','1-1'],
    ['捷克','南非',1.61,3.40,5.20,-0.75,2.5,'D','1-1'],
    ['厄瓜多尔','库拉索',1.19,6.10,12.5,-1.75,2.75,'D','0-0'],
]

def compute_signals(m):
    home, away = m[0], m[1]
    oh, od, oa = m[2], m[3], m[4]
    hcp, ou = m[5], m[6]
    act, score = m[7], m[8]
    
    total = 1/oh + 1/od + 1/oa
    ph = 1/oh/total; pd = 1/od/total; pa = 1/oa/total
    spread = abs(ph - pa)
    max_imp = max(ph, pa)
    fav_side = 'H' if ph > pa else 'A'
    fav_prob = max_imp
    
    # Parse score
    parts = score.split('-')
    h_goals = int(parts[0]); a_goals = int(parts[1])
    total_goals = h_goals + a_goals
    margin = abs(h_goals - a_goals)
    winner_side = 'H' if h_goals > a_goals else ('A' if a_goals > h_goals else 'D')
    
    # S7: OU/HCP — 屠杀指数核心
    s7 = ou / max(abs(hcp), 0.25)
    
    # S1: draw便宜度
    s1 = od / math.sqrt(oh * oa)
    
    # S3: HCP偏离 — 实际让球 vs 赔率隐含
    if fav_side == 'H':
        expected_hcp = -math.log(max_imp / min(ph, pa)) * 1.2
    else:
        expected_hcp = math.log(max_imp / min(ph, pa)) * 1.2
    s3_hcp_dev = abs(hcp) - abs(expected_hcp)
    
    # 屠杀指标: favor概率差 x OU/HCP比
    # 高值 = favorite很明确 + 进球潜力大 = 屠杀潜力
    slaughter_index = spread * s7
    
    # 比分偏差: 实际进球 vs OU预期
    goals_vs_ou = total_goals / ou
    
    # 胜率差距: favorite赢了多少
    if winner_side == 'H':
        win_margin = h_goals - a_goals
    elif winner_side == 'A':
        win_margin = a_goals - h_goals
    else:
        win_margin = 0
    
    return {
        'match': f'{home}vs{away}', 'score': score, 'total_goals': total_goals,
        'margin': margin, 'ph': ph, 'pd': pd, 'pa': pa, 'spread': spread,
        'max_imp': max_imp, 'fav_side': fav_side,
        's7': s7, 's1': s1, 's3': s3_hcp_dev,
        'slaughter_index': slaughter_index, 'goals_vs_ou': goals_vs_ou,
        'win_margin': win_margin, 'winner_side': winner_side,
        'ou': ou, 'hcp': hcp, 'oh': oh, 'od': od, 'oa': oa,
    }

def main():
    print("=" * 90)
    print("🔬 v5.1 12个误判 — 进球数分层深度分析")
    print("=" * 90)
    
    fp_signals = [compute_signals(m) for m in FALSE_POSITIVES]
    draw_signals = [compute_signals(m) for m in TRUE_DRAWS]
    
    # ═══ 分层 ═══
    low_goals_fp = [f for f in fp_signals if f['total_goals'] <= 2]  # 差点平局
    high_goals_fp = [f for f in fp_signals if f['total_goals'] >= 3]  # 真屠杀
    
    print(f"\n📊 分层结果:")
    print(f"  低进球(1-2)误判: {len(low_goals_fp)}场 — 差点平局, 预测合理")
    print(f"  高进球(3+)误判: {len(high_goals_fp)}场 — 真屠杀, 需要过滤")
    print(f"  真平局(对比): {len(draw_signals)}场")
    
    # ═══ 低进球误判 ═══
    if low_goals_fp:
        print(f"\n🟡 低进球误判 ({len(low_goals_fp)}场): 预测'平局'但低分差, 预测方向正确:")
        for f in low_goals_fp:
            print(f"  {f['match']} ({f['score']}): {f['total_goals']}球, 分差={f['margin']} "
                  f"pD={f['pd']:.1%} spread={f['spread']:.3f}")
    
    # ═══ 高进球误判深度分析 ═══
    print(f"\n{'='*90}")
    print(f"🔴 高进球误判 ({len(high_goals_fp)}场) — 屠杀信号分析")
    print(f"{'='*90}")
    
    # Build comparison stats
    print(f"\n  指标对比: 真平局 vs 高进球误判")
    print(f"  {'─'*70}")
    
    for name, key in [
        ('屠杀指数(spread×S7)', 'slaughter_index'),
        ('S7: OU/HCP比', 's7'),
        ('S1: draw便宜度', 's1'),
        ('Goals vs OU', 'goals_vs_ou'),
        ('实际分差', 'win_margin'),
        ('隐含概率spread', 'spread'),
        ('max_imp', 'max_imp'),
    ]:
        d_vals = [d[key] for d in draw_signals]
        h_vals = [f[key] for f in high_goals_fp]
        d_m = sum(d_vals)/len(d_vals) if d_vals else 0
        h_m = sum(h_vals)/len(h_vals) if h_vals else 0
        print(f"  {name:<25} 真平局={d_m:.3f}  屠杀={h_m:.3f}  Δ={h_m-d_m:+.3f}")
    
    # ═══ 逐场屠杀信号 ═══
    print(f"\n{'='*90}")
    print(f"🔴 高进球误判逐场详情 (按屠杀指数排序)")
    print(f"{'='*90}")
    print(f"  {'比赛':<25} {'比分':>6} {'pD':>6} {'S7':>6} {'屠杀指数':>8} {'goals/OU':>8}")
    print(f"  {'─'*65}")
    
    for f in sorted(high_goals_fp, key=lambda x: x['slaughter_index'], reverse=True):
        print(f"  {f['match']:<25} {f['score']:>6} {f['pd']:>6.1%} {f['s7']:>6.1f} "
              f"{f['slaughter_index']:>8.3f} {f['goals_vs_ou']:>8.2f}")
    
    # ═══ 真平局 vs 屠杀: 2x2对比 ═══
    print(f"\n{'='*90}")
    print(f"🎯 关键2x2对比: 同赔率结构的平局 vs 屠杀")
    print(f"{'='*90}")
    
    pairs = [
        ('荷兰vs日本', '2-2', '荷兰vs瑞典', '5-1'),
        ('巴西vs摩洛哥', '1-1', '比利时vs埃及', '1-1'),  # both draws
        ('加拿大vs波黑', '1-1', '加拿大vs卡塔尔', '6-0'),
        ('捷克vs南非', '1-1', '瑞士vs波黑', '4-1'),
    ]
    
    for d_name, d_score, f_name, f_score in pairs:
        d = next((s for s in draw_signals if s['match'] == d_name), None)
        f = next((s for s in high_goals_fp if s['match'] == f_name), fp_signals[0])
        # For 2nd pair (both draws), f is also a draw
        if d_name == '巴西vs摩洛哥':
            f = next((s for s in draw_signals if s['match'] == '比利时vs埃及'), None)
        
        if d and f:
            print(f"\n  ✅ {d_name} ({d_score}平)  vs  ❌ {f_name} ({f_score}屠杀)")
            print(f"     赔率: {d['oh']}/{d['od']}/{d['oa']} vs {f['oh']}/{f['od']}/{f['oa']}")
            print(f"     屠杀指数: {d['slaughter_index']:.3f} vs {f['slaughter_index']:.3f} Δ={f['slaughter_index']-d['slaughter_index']:+.3f}")
            print(f"     S7(OU/HCP): {d['s7']:.1f} vs {f['s7']:.1f}")
            print(f"     S1(draw便宜): {d['s1']:.3f} vs {f['s1']:.3f}")
            print(f"     实际进球: {d['total_goals']}球 vs {f['total_goals']}球")
    
    # ═══ 屠杀过滤策略 ═══
    print(f"\n{'='*90}")
    print(f"💡 屠杀过滤策略建议")
    print(f"{'='*90}")
    
    # How many FPs would be caught by strict S7+S1?
    print(f"\n  S7+S1组合: S7≥5.0 AND S1<1.20 → 屠杀硬证据")
    caught_by_s7s1 = [f for f in high_goals_fp if f['s7'] >= 5.0 and f['s1'] < 1.20]
    print(f"  可过滤: {len(caught_by_s7s1)}场误判")
    for f in caught_by_s7s1:
        wins_by = f['win_margin']
        print(f"    {f['match']} ({f['score']}): +{wins_by}球屠杀, S7={f['s7']:.1f} S1={f['s1']:.3f}")
    
    # How many draws would be lost?
    killed_draws = [d for d in draw_signals if d['s7'] >= 5.0 and d['s1'] < 1.20]
    print(f"\n  会牺牲真平局: {len(killed_draws)}场")
    for d in killed_draws:
        print(f"    {d['match']} ({d['score']})")
    
    # Alternative: slaughter_index threshold
    print(f"\n  屠杀指数阈值: slaughter_index > 0.40 → 屠杀预警")
    caught_by_si = [f for f in high_goals_fp if f['slaughter_index'] > 0.40]
    print(f"  可过滤: {len(caught_by_si)}场误判")
    killed_draws_si = [d for d in draw_signals if d['slaughter_index'] > 0.40]
    print(f"  会牺牲真平局: {len(killed_draws_si)}场")

if __name__ == "__main__":
    main()
