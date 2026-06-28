#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
R3赛后一键更新器 — 录入赛果 → 更新积分 → 预测16强
=====================================================
用法:
  python pipeline/r3_post_match.py                            # 交互录入
  python pipeline/r3_post_match.py --batch results.json        # 批量录入
  python pipeline/r3_post_match.py --auto                      # 自动从DB读取+预测
"""

import sys, json, argparse
from pathlib import Path

ARCH = Path(__file__).resolve().parent.parent

from data.dynamic_team_db_module import DynamicTeamDB

def update_standings_from_results(results: dict):
    """更新DynamicTeamDB积分"""
    db = DynamicTeamDB()
    db.load(force_reload=False)
    
    for (home, away), (hg, ag) in results.items():
        # 找到匹配的球队
        home_data = db.get_team(home)
        away_data = db.get_team(away)
        
        if not home_data.get('gp'):
            continue
            
        # 更新积分
        for team, gf, ga, is_home in [(home, hg, ag, True), (away, ag, hg, False)]:
            data = db._db.get(team, {})
            gp = data.get('gp', 0) + 1
            old_gf = data.get('gf', 0)
            old_ga = data.get('ga', 0)
            old_pts = data.get('pts', 0)
            
            new_pts = old_pts
            if gf > ga: new_pts += 3
            elif gf == ga: new_pts += 1
            
            data.update({
                'gp': gp, 'gf': old_gf + gf, 'ga': old_ga + ga,
                'pts': new_pts,
                'avg_gf': round((old_gf + gf) / gp, 2),
                'avg_ga': round((old_ga + ga) / gp, 2),
                'pts_per_game': round(new_pts / gp, 2),
            })
            data['results'] = data.get('results', []) + [
                {'opp': away if is_home else home, 'gf': gf, 'ga': ga}
            ]
        
        home_result = 'H' if hg > ag else ('D' if hg == ag else 'A')
        print(f'  ✅ {home} {hg}-{ag} {away} ({home_result}) → {home}={home_data.get("pts",0)}pts {away_data.get("pts",0)}pts')
    
    # 保存
    try:
        with open(ARCH / 'data' / 'dynamic_team_db.json', 'w', encoding='utf-8') as f:
            json.dump(db._db, f, ensure_ascii=False, indent=2)
        print(f'\n  💾 积分已保存: data/dynamic_team_db.json')
    except (OSError, IOError) as e:
        print(f'\n  ⚠️ 保存失败: {e}')
    
    return db

# R3赛程模板（6/28 6场）
R3_TEMPLATE = {
    ('克罗地亚', '加纳'): None,
    ('巴拿马', '英格兰'): None,
    ('哥伦比亚', '葡萄牙'): None,
    ('民主刚果', '乌兹别克斯坦'): None,
    ('阿尔及利亚', '奥地利'): None,
    ('约旦', '阿根廷'): None,
}

def main():
    ap = argparse.ArgumentParser(description='R3赛后一键更新')
    ap.add_argument('--batch', help='JSON文件批量录入: {"克罗地亚:加纳": [2,0], ...}')
    ap.add_argument('--interactive', action='store_true', help='交互录入')
    ap.add_argument('--skip-predict', action='store_true', help='跳过淘汰赛预测')
    args = ap.parse_args()

    if args.batch:
        with open(args.batch, 'r', encoding='utf-8') as f:
            results = json.load(f)
        # Parse key format "home:away" or list format
        parsed = {}
        for k, v in results.items():
            if isinstance(k, str) and ':' in k:
                h, a = k.split(':')
                parsed[(h, a)] = tuple(v)
        update_standings_from_results(parsed)
    elif args.interactive:
        print(f'\n  📝 R3赛果录入 (输入实际比分, 格式: 2-0 或 3-1)\n')
        batch = {}
        for (home, away), _ in R3_TEMPLATE.items():
            score = input(f'  {home} vs {away}: ').strip()
            if score:
                try:
                    hg, ag = map(int, score.split('-'))
                    batch[(home, away)] = (hg, ag)
                except (ValueError, TypeError):
                    print(f'    格式错误, 跳过')
        if batch:
            update_standings_from_results(batch)
    
    # 运行淘汰赛预测
    if not args.skip_predict:
        import subprocess
        python = str(ARCH / '.venv' / 'Scripts' / 'python.exe')
        print(f'\n  ⚡ 运行淘汰赛预测...')
        subprocess.run(
            [python, str(ARCH / 'pipeline' / 'knockout_predictor.py')],
            env={**__import__('os').environ, 'PYTHONPATH': str(ARCH), 'PYTHONIOENCODING': 'utf-8', 'SECRET_KEY': 'dev'}
        )
    
    print(f'\n  🟢 R3更新完成')

if __name__ == '__main__':
    main()
