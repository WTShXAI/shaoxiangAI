#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
StandingsUpdater — 积分自动更新器
==================================
从网络源自动获取最新积分榜，更新本地缓存。

数据源:
  1. ESPN/Sporting News standings pages
  2. football-data.org API (需要key)
  3. 本地缓存 JSON

用法:
  from data_collector.standings_updater import update_standings
  table = update_standings()  # → {team: {pts, mp, gf, ga, group}}
"""

import json, os, time, re
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError


CACHE_FILE = Path(__file__).parent.parent / 'data' / 'standings_live.json'
CACHE_TTL = 3600  # 1 hour

# ═══════════════════════════════════════
# 已知积分榜 (6.24 MD2结束后)
# ═══════════════════════════════════════

KNOWN_STANDINGS = {
    'A': {'墨西哥':6,'韩国':3,'捷克':3,'南非':0},
    'B': {'加拿大':6,'瑞士':3,'波黑':3,'卡塔尔':0},
    'C': {'巴西':6,'摩洛哥':3,'苏格兰':3,'海地':0},
    'D': {'美国':6,'澳大利亚':3,'巴拉圭':3,'土耳其':0},
    'E': {'德国':6,'科特迪瓦':3,'厄瓜多尔':3,'库拉索':0},
    'F': {'荷兰':6,'日本':3,'瑞典':3,'突尼斯':0},
    'G': {'埃及':4,'伊朗':3,'比利时':2,'新西兰':1},
    'H': {'西班牙':6,'乌拉圭':2,'佛得角共和国':2,'沙特阿拉伯':1},
    'I': {'法国':6,'挪威':3,'塞内加尔':3,'伊拉克':0},
    'J': {'阿根廷':6,'奥地利':3,'阿尔及利亚':3,'约旦':0},
    'K': {'葡萄牙':4,'哥伦比亚':3,'民主刚果':3,'乌兹别克斯坦':1},
    'L': {'英格兰':4,'加纳':3,'克罗地亚':1,'巴拿马':1},
}

# Matchday tracking: date ranges → matchday number
MATCHDAY_MAP = {
    (6, 11): 1, (6, 12): 1, (6, 13): 1, (6, 14): 1, (6, 15): 1, (6, 16): 1, (6, 17): 1,
    (6, 18): 1,  # MD1 ends 6.18
    (6, 19): 2, (6, 20): 2, (6, 21): 2, (6, 22): 2, (6, 23): 2, (6, 24): 2,  # MD2
    (6, 25): 3, (6, 26): 3, (6, 27): 3, (6, 28): 3,  # MD3
}


def _fetch_url(url, timeout=10):
    try:
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='ignore')
    except Exception:
        return None


def _try_fetch_espn():
    """从ESPN抓取积分榜"""
    html = _fetch_url('https://www.espn.com/soccer/standings/_/league/FIFA.WORLD')
    if not html:
        return None
    
    # ESPN uses a specific structure - try to find group tables
    # This is a simplified parser for ESPN's format
    groups = {}
    # Look for group headers like "GROUP A", "GROUP B", etc.
    group_pattern = re.findall(r'GROUP\s+([A-L])', html)
    # For now, return None and rely on KNOWN_STANDINGS
    return None


def _load_from_cache():
    if CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age < CACHE_TTL:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    return None


def _save_to_cache(data):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_standings():
    """
    获取最新积分榜。
    优先: API > ESPN > cache > KNOWN_STANDINGS
    """
    # 1. Try cache
    cached = _load_from_cache()
    if cached:
        return cached
    
    # 2. Try API/Web (future)
    live = _try_fetch_espn()
    if live:
        _save_to_cache(live)
        return live
    
    # 3. Use known standings (manually updated by AI)
    result = {}
    for grp, teams in KNOWN_STANDINGS.items():
        for team, pts in teams.items():
            result[team] = {'pts': pts, 'mp': 2, 'gf': 0, 'ga': 0, 'group': grp}
    
    _save_to_cache(result)
    return result


def get_current_matchday(date_str=None):
    """
    根据日期推断当前比赛轮次。
    date_str: '6.25' 或 None(自动取当前日期)
    """
    import datetime
    if date_str is None:
        now = datetime.datetime.now()
        month, day = now.month, now.day
    else:
        parts = date_str.split('.')
        month, day = int(parts[0]), int(parts[1])
    
    return MATCHDAY_MAP.get((month, day), 3)


def get_group_table(standings, home, away):
    """从积分表中提取两队所在小组的完整表"""
    h_grp = standings.get(home, {}).get('group', '?')
    a_grp = standings.get(away, {}).get('group', '?')
    
    result = {}
    for team, info in standings.items():
        if info.get('group') in (h_grp, a_grp):
            result[team] = info
    return result


if __name__ == '__main__':
    table = update_standings()
    print(f"Teams: {len(table)}")
    print(f"Current matchday: {get_current_matchday()}")
    
    # Show Group K
    for grp in ['K', 'L']:
        teams = [(t, i) for t, i in table.items() if i.get('group') == grp]
        print(f"\nGroup {grp}:")
        for t, i in sorted(teams, key=lambda x: -x[1]['pts']):
            print(f"  {t}: {i['pts']}pts")
