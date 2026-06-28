#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
StandingsProvider — 可插拔赛事数据源
======================================

设计目标: 从根本上解决数据获取问题, 不依赖单一缓存或API。

三级数据源 (自动降级):
  Level 1: football-data.org API   → 实时数据
  Level 2: 本地 JSON 缓存          → 离线可用
  Level 3: 用户手动配置            → 任何赛事零门槛起步

输出统一格式: GroupTable
  {team_name: {'pts': int, 'mp': int, 'gf': int, 'ga': int, 'group': str}}

使用方式:
    from rules.standings_provider import StandingsProvider

    sp = StandingsProvider()
    table, matchday = sp.get_context('葡萄牙', '哥伦比亚')
    # → ({葡萄牙:{pts:4,mp:2,...}, 哥伦比亚:{pts:3,mp:2,...}}, 2)
"""

import json, os
from pathlib import Path

# ═══════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════

GroupTable = dict  # {team_name: {pts, mp, gf, ga, group}}

# ═══════════════════════════════════════
# Level 3: 手动配置 (任何赛事的兜底)
# ═══════════════════════════════════════

def load_manual_config(path=None):
    """读取用户手写的积分配置 JSON。"""
    if path is None:
        path = Path(__file__).parent.parent / 'config' / 'standings.json'
    if not os.path.exists(path):
        return None

    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    table = {}
    matchday = data.get('matchday', 1)

    groups = data.get('groups', {})
    for group_name, teams in groups.items():
        for t in teams:
            name = t['team']
            table[name] = {
                'pts': t.get('pts', 0),
                'mp': t.get('mp', 0),
                'gf': t.get('gf', 0),
                'ga': t.get('ga', 0),
                'group': group_name,
            }

    return table, matchday

# ═══════════════════════════════════════
# Level 2: 本地缓存 (WC2026 预装数据)
# ═══════════════════════════════════════

def load_cache():
    """从 football-data.org API 缓存文件构建 GroupTable。

    使用两个文件:
      - standings: 48队总表 (team, pts, mp, gf, ga)
      - matches:   104场比赛 (group, matchday)
    """
    cache_dir = Path(__file__).parent.parent / 'data' / 'api_cache'
    standings_file = cache_dir / '_competitions_WC_standings_season_2026.json'
    matches_file = cache_dir / '_competitions_WC_matches_season_2026.json'

    if not standings_file.exists() or not matches_file.exists():
        return None, None

    with open(standings_file, 'r', encoding='utf-8') as f:
        standings_data = json.load(f)
    with open(matches_file, 'r', encoding='utf-8') as f:
        matches_data = json.load(f)

    # ── Build team→group map from match data ──
    team_group = {}
    for m in matches_data.get('matches', []):
        group = m.get('group', '')
        if not group:
            continue
        # Normalize: "GROUP_A" → "A"
        group_short = group.replace('GROUP_', '')

        ht_name = _normalize_name(m.get('homeTeam', {}).get('name', ''))
        at_name = _normalize_name(m.get('awayTeam', {}).get('name', ''))
        if ht_name:
            team_group[ht_name] = group_short
        if at_name:
            team_group[at_name] = group_short

    # ── Get matchday ──
    matchday = matches_data.get('season', {}).get('currentMatchday', 1)

    # ── Build group table from standings ──
    table = {}
    for s in standings_data.get('standings', []):
        for entry in s.get('table', []):
            team_info = entry.get('team', {})
            name = _normalize_name(team_info.get('name', ''))

            group = team_group.get(name, '?')

            table[name] = {
                'pts': entry.get('points', 0),
                'mp': entry.get('playedGames', 0),
                'gf': int(entry.get('goalsFor', 0)),
                'ga': int(entry.get('goalsAgainst', 0)),
                'group': group,
            }

    return table, matchday

# ═══════════════════════════════════════
# Level 1: API (football-data.org) — 未来扩展
# ═══════════════════════════════════════

def load_api():
    """调用 football-data.org API 获取实时数据。需要 API_KEY 环境变量。"""
    api_key = os.environ.get('FOOTBALL_DATA_API_KEY')
    if not api_key:
        return None, None
    # TODO: 实现 API 调用
    return None, None

# ═══════════════════════════════════════
# 名称标准化
# ═══════════════════════════════════════

_NAME_MAP = {
    'Czechia': '捷克',
    'Bosnia-Herzegovina': '波黑',
    'Congo DR': '民主刚果',
    'Korea Republic': '韩国',
    'South Korea': '韩国',
}

def _normalize_name(name):
    """统一中文名。"""
    if not name:
        return ''
    return _NAME_MAP.get(name, name)

# ═══════════════════════════════════════
# 主入口
# ═══════════════════════════════════════

class StandingsProvider:
    """赛事数据提供器: API → 缓存 → 手动配置 三级降级。"""

    def __init__(self, manual_config_path=None):
        self._table = None
        self._matchday = 1
        self._source = 'none'
        self._manual_path = manual_config_path
        self._load()

    def _load(self):
        """按优先级尝试加载数据。"""
        # Level 1: API
        table, md = load_api()
        if table:
            self._table = table
            self._matchday = md
            self._source = 'api'
            return

        # Level 2: 缓存
        table, md = load_cache()
        if table:
            self._table = table
            self._matchday = md
            self._source = 'cache'
            return

        # Level 3: 手动配置
        table, md = load_manual_config(self._manual_path)
        if table:
            self._table = table
            self._matchday = md
            self._source = 'manual'
            return

        # 完全无数据
        self._table = {}
        self._matchday = 1
        self._source = 'none'

    @property
    def available(self):
        return len(self._table) > 0

    @property
    def matchday(self):
        return self._matchday

    @property
    def source(self):
        return self._source

    def get_team(self, name):
        """获取单队数据。"""
        return self._table.get(name, {})

    def get_group_table(self, home, away):
        """
        获取两队所在小组的完整积分表。
        返回: {team_name: {pts, mp, gf, ga, group}}
        """
        h_info = self._table.get(home, {})
        a_info = self._table.get(away, {})

        group_h = h_info.get('group', '?')
        group_a = a_info.get('group', '?')

        # 如果两队不同组，合并两张表
        groups_to_include = {group_h, group_a}

        result = {}
        for team, info in self._table.items():
            if info.get('group', '?') in groups_to_include:
                result[team] = info
        return result

    def get_context(self, home, away):
        """
        一站式获取赛事上下文。
        返回: (group_table, matchday)
        """
        return self.get_group_table(home, away), self._matchday

    def dump(self):
        """打印当前数据状态。"""
        print(f'StandingsProvider: source={self._source}, matchday={self._matchday}, teams={len(self._table)}')
        if self._table:
            groups = {}
            for name, info in self._table.items():
                g = info.get('group', '?')
                groups.setdefault(g, []).append((name, info['pts']))
            for g, teams in sorted(groups.items()):
                print(f'  Group {g}:')
                for t, p in sorted(teams, key=lambda x: -x[1]):
                    print(f'    {t}: {p}pts')

# ═══════════════════════════════════════
# 全局单例
# ═══════════════════════════════════════

_provider = None

def get_provider():
    global _provider
    if _provider is None:
        _provider = StandingsProvider()
    return _provider

# ═══════════════════════════════════════
# 快速测试
# ═══════════════════════════════════════

if __name__ == '__main__':
    sp = StandingsProvider()
    sp.dump()

    # Test get_context
    table, md = sp.get_context('葡萄牙', '哥伦比亚')
    print(f'\nMatchday: {md}')
    print('Group table:')
    for t, i in sorted(table.items(), key=lambda x: -x[1]['pts']):
        print(f'  {t}: {i["pts"]}pts MP{i["mp"]} GF{i["gf"]} GA{i["ga"]}')
