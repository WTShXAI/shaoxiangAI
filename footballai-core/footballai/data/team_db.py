"""
核心数据层 — 球队积分 + 实力分级 + 侦察报告
=============================================
独立于项目路径，接受外部队列传入
"""

import json, math
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional


class TeamDB:
    """核心球队数据库 — 接收外部数据，提供查询+分级"""

    def __init__(self, teams_data: Dict = None, fifa_ranks: Dict = None):
        self._teams = teams_data or {}
        self._fifa = fifa_ranks or {}

    def load_from_sources(self, match_results, standings_data, fifa_path=None):
        """从原始数据构建数据库"""
        if fifa_path:
            with open(fifa_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
                raw.pop('_meta', None)
                for k, v in raw.items():
                    self._fifa[k] = v if isinstance(v, int) else v.get('rank', 99)

        # 从比赛结果计算积分
        teams = defaultdict(lambda: {'pts': 0, 'gp': 0, 'gf': 0, 'ga': 0, 'results': []})
        for h, a, hg, ag, md in match_results:
            teams[h]['pts'] += 3 if hg > ag else (1 if hg == ag else 0)
            teams[a]['pts'] += 3 if ag > hg else (1 if hg == ag else 0)
            teams[h]['gp'] += 1; teams[a]['gp'] += 1
            teams[h]['gf'] += hg; teams[h]['ga'] += ag
            teams[a]['gf'] += ag; teams[a]['ga'] += hg
            teams[h]['results'].append({'opp': a, 'gf': hg, 'ga': ag})
            teams[a]['results'].append({'opp': h, 'gf': ag, 'ga': hg})

        # 覆盖standings数据(更权威)
        for t, s in (standings_data or {}).items():
            if isinstance(s, dict):
                teams[t].update(s)

        # 计算档位和avg
        self._teams = {}
        for t, d in teams.items():
            gp = max(d['gp'], 1)
            self._teams[t] = {
                **d,
                'team': t,
                'tier': self._assign_tier(d['pts'], gp),
                'avg_gf': round(d['gf'] / gp, 2),
                'avg_ga': round(d['ga'] / gp, 2),
                'pts_per_game': round(d['pts'] / gp, 2),
                'scout_pattern': self._generate_scout(t, d['results'])[0],
                'scout_weakness': self._generate_scout(t, d['results'])[1],
                'fifa_rank': self._fifa.get(t, 99),
            }

    def get(self, team: str, default=None):
        d = self._teams.get(team)
        if d: return d
        # fuzzy match
        for k in self._teams:
            if team in k or k in team:
                return self._teams[k]
        return default or {}

    def get_elo(self, team: str, base=1500):
        rank = self._fifa.get(team, 50)
        return 2000 - (rank - 1) * 6

    @staticmethod
    def _assign_tier(pts, gp):
        ppg = pts / max(gp, 1)
        if ppg >= 2.5: return 1
        if ppg >= 1.5: return 2
        if ppg >= 0.5: return 3
        return 4

    @staticmethod
    def _generate_scout(team, results):
        if not results:
            return ('无数据', '无数据')
        n = len(results)
        w = sum(1 for r in results if r['gf'] > r['ga'])
        d = sum(1 for r in results if r['gf'] == r['ga'])
        l = sum(1 for r in results if r['gf'] < r['ga'])
        avg_gf = sum(r['gf'] for r in results) / n
        avg_ga = sum(r['ga'] for r in results) / n
        total_g = avg_gf + avg_ga

        if w >= n * 0.7:
            pattern = f'强势队: {n}场{w}胜{d}平{l}负, 场均{avg_gf:.1f}球'
        elif d >= n * 0.5:
            pattern = f'平局大师: {n}场{d}平, 场均{avg_gf:.1f}球'
        elif avg_gf >= 2.5:
            pattern = f'进攻狂: {n}场进{avg_gf*n:.0f}球, 场均{avg_gf:.1f}'
        elif avg_ga <= 0.5:
            pattern = f'铁壁防守: {n}场仅失{avg_ga*n:.0f}球'
        elif total_g <= 2.0:
            pattern = f'小比分型: 场均{total_g:.1f}球'
        else:
            pattern = f'均衡型: {n}场{w}胜{d}平{l}负, 场均{avg_gf:.1f}进/{avg_ga:.1f}失'

        weaknesses = []
        if avg_ga >= 2.0:
            weaknesses.append(f'防守漏洞(场均失{avg_ga:.1f})')
        if avg_gf <= 0.5:
            weaknesses.append(f'进攻哑火(场均进{avg_gf:.1f})')
        if l >= 2:
            weaknesses.append(f'连败({l}负)')
        if not weaknesses:
            weaknesses.append('无明显短板')
        return (pattern, '; '.join(weaknesses))
