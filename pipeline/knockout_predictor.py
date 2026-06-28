#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
v5.7 淘汰赛晋级预测器 — 从小组赛预测到冠军
============================================
基于: 当前积分 + 未来赛程 + Elo实力 + 全链路预测

用法:
  python pipeline/knockout_predictor.py          # 打印所有预测
  python pipeline/knockout_predictor.py --json   # JSON输出
"""

import sys, json, math
from pathlib import Path
from collections import defaultdict

ARCH_ROOT = Path(__file__).resolve().parent.parent

GROUPS = ['A','B','C','D','E','F','G','H','I','J','K','L']

# 淘汰赛对阵: 小组头名vs第二名 (12组→16强后还需补4个最好第三名)
# 2026扩军48队: 12组×4队, 每组前2(24队)+8个最好第三名→32强→16强→8强→4强→决赛
# 简化版: 12组前2 = 24队 + 8个最好第三名 = 32强

def load_standings():
    """从DynamicTeamDB获取所有球队积分"""
    from data.dynamic_team_db_module import DynamicTeamDB
    DynamicTeamDB.load()
    return DynamicTeamDB._db

def get_group_from_map(team_name, group_map):
    """查找球队所在小组"""
    for g, teams in group_map.items():
        if team_name in teams:
            return g
    return '?'

def predict_r3(matches_dict, group_standings):
    """预测小组赛R3结果并返回最终积分榜
    用Elo估算R3结果, 结合当前积分计算最终排名
    """
    elos = _build_elo()
    final_standings = defaultdict(lambda: {'pts': 0, 'gf': 0, 'ga': 0, 'gp': 0})
    
    # 先加载当前积分
    for team, data in group_standings.items():
        if isinstance(data, dict) and data.get('gp', 0) > 0:
            g = '?'  # 需外部传入小组映射
            final_standings[f'{g}_{team}'] = {
                'pts': data['pts'], 'gf': data.get('gf', 0),
                'ga': data.get('ga', 0), 'gp': data['gp']
            }
    
    return final_standings

def _build_elo():
    """从 ALL_RESULTS 构建Elo"""
    from rules.d_gate_utils import ALL_RESULTS
    from rules.drawgate_v53 import imp_from_odds
    
    fifa = {}
    try:
        with open(str(ARCH_ROOT / 'config' / 'fifa_rankings_2026.json'), encoding='utf-8') as f:
            fifa_data = json.load(f)
            fifa_data.pop('_meta', None)
            for k, v in fifa_data.items():
                rank = v if isinstance(v, (int, float)) else v.get('rank', 50)
                fifa[k] = rank
    except (json.JSONDecodeError, FileNotFoundError):
        pass
    
    fe = lambda t: 2000 - (fifa.get(t, 50)-1)*6
    elo = {t: fe(t) for t in fifa}
    K = 32
    for h, a, hg, ag, hcp, _ in ALL_RESULTS:
        if h not in elo or a not in elo:
            continue
        rh, ra = elo[h], elo[a]
        eh = 1/(1+10**((ra-rh)/400))
        sh = 1.0 if hg>ag else (0.5 if hg==ag else 0)
        gm = 1 + (abs(hg-ag)-1)*0.5 if abs(hg-ag)>1 else 1
        elo[h] = rh + K*gm*(sh-eh)
        elo[a] = ra + K*gm*((1-sh)-(1-eh))
    return elo

def predict_group_outcome(group_teams, current_pts, elo, r3_matches):
    """预测小组最终排名
    group_teams: 4队名单
    current_pts: {team: {'pts': N, 'gf': N, 'ga': N}}
    r3_matches: [(home, away), ...] 本组R3赛程
    """
    # 简化版: 用Elo差估算R3方向
    pts = {t: current_pts.get(t, {}).get('pts', 0) for t in group_teams}
    gf = {t: current_pts.get(t, {}).get('gf', 0) for t in group_teams}
    ga = {t: current_pts.get(t, {}).get('ga', 0) for t in group_teams}
    
    for home, away in r3_matches:
        eh, ea = elo.get(home, 1500), elo.get(away, 1500)
        d = eh - ea
        prob_h = 1/(1+10**(-d/400))
        # 简单预测: prob_h>0.6 → 主胜, prob_h<0.4 → 客胜, 否则平
        if prob_h > 0.6:
            pts[home] += 3
            gf[home] += 2; ga[away] += 1
            ga[home] += 1; gf[away] += 0
        elif prob_h < 0.4:
            pts[away] += 3
            ga[home] += 1; gf[away] += 2
            gf[home] += 0; ga[away] += 1
        else:
            pts[home] += 1; pts[away] += 1
            gf[home] += 1; ga[away] += 1
            gf[away] += 1; ga[home] += 1
    
    # 排序
    teams_sorted = sorted(group_teams, key=lambda t: (pts[t], gf[t]-ga[t], gf[t]), reverse=True)
    return teams_sorted  # [第1名, 第2名, 第3名, 第4名]

def build_tournament_predictions(group_map, group_standings, elo, r3_schedule):
    """完整晋级路线预测"""
    # 1. 小组出线预测
    r16 = []  # 32支晋级队伍
    third_place = []  # 第三名排名
    
    for g_name, teams in group_map.items():
        r3 = r3_schedule.get(g_name, [])
        ranking = predict_group_outcome(teams, group_standings, elo, r3)
        r16.extend([(ranking[0], g_name, 1), (ranking[1], g_name, 2)])
        third_place.append((ranking[2], g_name, ranking[2] in teams and group_standings.get(ranking[2], {}).get('pts', 0)))
    
    # 8个最好第三名
    third_place.sort(key=lambda x: -group_standings.get(x[0], {}).get('pts', 0) if x[0] in group_standings else 0)
    best_third = [t[0] for t in third_place[:8]]
    
    # 32强名单
    r32 = r16 + [(t, '?', 3) for t in best_third]
    
    # 2. 16强对阵 + 预测
    # 2026赛制: 12组前2(24)+8个最好第三=32强
    # 简化处理: 按Elo排名配对
    r32_sorted = sorted(r32, key=lambda x: -elo.get(x[0], 1500))
    r16_matchups = []
    for i in range(16):
        strong = r32_sorted[i][0]
        weak = r32_sorted[31-i][0]
        es, ew = elo.get(strong, 1500), elo.get(weak, 1500)
        prob_s = 1/(1+10**((ew-es)/400))
        r16_matchups.append((strong, weak, prob_s, '晋级' if prob_s > 0.5 else '淘汰'))
    
    # 3. 8强预测
    qf = [m[0] for m in r16_matchups if m[3] == '晋级']
    qf_matchups = []
    for i in range(0, len(qf), 2):
        if i+1 < len(qf):
            es, ew = elo.get(qf[i], 1500), elo.get(qf[i+1], 1500)
            prob_s = 1/(1+10**((ew-es)/400))
            qf_matchups.append((qf[i], qf[i+1], prob_s, qf[i] if prob_s > 0.5 else qf[i+1]))
    
    # 4. 4强预测
    sf = [m[3] for m in qf_matchups]
    sf_matchups = []
    for i in range(0, len(sf), 2):
        if i+1 < len(sf):
            es, ew = elo.get(sf[i], 1500), elo.get(sf[i+1], 1500)
            prob_s = 1/(1+10**((ew-es)/400))
            sf_matchups.append((sf[i], sf[i+1], prob_s, sf[i] if prob_s > 0.5 else sf[i+1]))
    
    # 5. 决赛预测
    finalists = [m[3] for m in sf_matchups]
    champion = None
    if len(finalists) >= 2:
        es, ew = elo.get(finalists[0], 1500), elo.get(finalists[1], 1500)
        champion = finalists[0] if es > ew else finalists[1]
    
    return {
        'round_of_32': [t[0] for t in r32],
        'round_of_16': r16_matchups,
        'quarter_finals': qf_matchups,
        'semi_finals': sf_matchups,
        'final': finalists,
        'champion': champion,
    }

def main():
    """主入口: 基于已知积分预测晋级"""
    import argparse
    ap = argparse.ArgumentParser(description='淘汰赛晋级预测器 v5.7')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    
    # 已确认的12组分组 (基于API赛程和DB)
    # 48队完整分组来自FIFA官方赛程
    ALL_GROUPS = {
        'A': ['墨西哥','南非','韩国','捷克'],
        'B': ['瑞士','波黑','加拿大','卡塔尔'],
        'C': ['巴西','摩洛哥','苏格兰','海地'],
        'D': ['美国','巴拉圭','土耳其','澳大利亚'],
        'E': ['德国','科特迪瓦','厄瓜多尔','库拉索'],
        'F': ['荷兰','日本','瑞典','突尼斯'],
        'G': ['比利时','埃及','伊朗','新西兰'],
        'H': ['西班牙','沙特阿拉伯','乌拉圭','佛得角'],
        'I': ['法国','塞内加尔','挪威','伊拉克'],
        'J': ['阿根廷','阿尔及利亚','奥地利','约旦'],
        'K': ['哥伦比亚','民主刚果','葡萄牙','乌兹别克斯坦'],
        'L': ['克罗地亚','加纳','英格兰','巴拿马'],
    }
    
    db = load_standings()
    elo = _build_elo()
    from data.dynamic_team_db_module import DynamicTeamDB
    
    # 从DB加载已知积分 (使用fuzzy match)
    group_standings = {}
    for g, teams in ALL_GROUPS.items():
        for t in teams:
            d = DynamicTeamDB.get_team(t)
            if d and d.get('gp', 0) > 0:
                group_standings[t] = d
    
    print(f'总球队: {len(ALL_GROUPS)*4} ({len(ALL_GROUPS)}组) | 有积分数据: {len(group_standings)}队\n')
    
    # 列出每组前2名 (基于当前积分, 不考虑净胜球)
    print('📊 当前小组排名 (积分):')
    for g, teams in sorted(ALL_GROUPS.items()):
        ranked = sorted(teams, key=lambda t: -group_standings.get(t, {}).get('pts', 0))
        pts_parts = []
        for t in ranked:
            p = group_standings.get(t, {}).get('pts', 0)
            pts_parts.append(f'{t}({p}分)')
        pts_str = ', '.join(pts_parts)
        print(f'  Group {g}: {pts_str}')
    
    # 小组出线预测 (有积分数据的才预测)
    print(f'\n🔮 R3模拟出线预测:')
    for g, teams in sorted(ALL_GROUPS.items()):
        has_data = sum(1 for t in teams if t in group_standings)
        if has_data >= 2:
            ranking = predict_group_outcome(teams, group_standings, elo, [])
            qualified = '→'.join(ranking[:2])
            elim = ', '.join(ranking[2:])
            print(f'  Group {g}: {qualified:40s} 淘汰: {elim}')
        else:
            print(f'  Group {g}: (数据不足, 无法预测)')
    
    # 晋级球队 (基于当前积分)
    qualified_teams = []
    for g, teams in sorted(ALL_GROUPS.items()):
        ranked = sorted(teams, key=lambda t: -group_standings.get(t, {}).get('pts', 0))
        if len(ranked) >= 2:
            qualified_teams.extend([(ranked[0], g, 1), (ranked[1], g, 2)])
    
    print(f'\n🔵 当前已确认出线球队 ({len(qualified_teams)//2}组):')
    for t, g, r in qualified_teams:
        pts = group_standings.get(t, {}).get('pts', 0)
        print(f'  Group {g} #{r}: {t} ({pts}分, Elo={elo.get(t,1500):.0f})')
    
    # 16强+预测 (仅对有数据的队伍)
    if len(qualified_teams) >= 4:
        sorted_q = sorted(qualified_teams, key=lambda x: -elo.get(x[0], 1500))
        print(f'\n🔶 模拟16强 (Elo配对):')
        n = min(len(sorted_q), 16)
        for i in range(n // 2):
            s_team = sorted_q[i][0]
            w_team = sorted_q[n-1-i][0]
            es, ew = elo.get(s_team, 1500), elo.get(w_team, 1500)
            prob = 1/(1+10**((ew-es)/400))
            winner = s_team if prob > 0.5 else w_team
            print(f'  {s_team:12s}({es:.0f}) vs {w_team:12s}({ew:.0f}) → {winner} ({max(prob,1-prob):.0%})')
    else:
        print('\n⚠️ 数据不足, 无法模拟16强')
    
    print(f'\n🏆 预测完成')
    print(f'  数据: {len(group_standings)}/48队有积分 | {len(elo)}队有Elo')
    print(f'  下次更新: R3结束后实况录入')

if __name__ == '__main__':
    main()
