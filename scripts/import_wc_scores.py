#!/usr/bin/env python
"""从 worldcup26.ir 缓存导入世界杯比分到 SQLite
用法: python scripts/import_wc_scores.py
"""
import json, sqlite3, sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent

# English → Chinese team name mapping
NAME_MAP = {
    'Mexico': '墨西哥', 'South Africa': '南非', 'South Korea': '韩国', 'Czech Republic': '捷克',
    'Czechia': '捷克', 'Switzerland': '瑞士', 'Canada': '加拿大',
    'Bosnia and Herzegovina': '波黑', 'Bosnia & Herzegovina': '波黑', 'Qatar': '卡塔尔',
    'Brazil': '巴西', 'Morocco': '摩洛哥', 'Scotland': '苏格兰', 'Haiti': '海地',
    'United States': '美国', 'Australia': '澳大利亚', 'Paraguay': '巴拉圭', 'Turkey': '土耳其',
    'Germany': '德国', 'Ivory Coast': '科特迪瓦', "Cote d'Ivoire": '科特迪瓦',
    'Ecuador': '厄瓜多尔', 'Curaçao': '库拉索', 'Curacao': '库拉索',
    'Netherlands': '荷兰', 'Japan': '日本', 'Sweden': '瑞典', 'Tunisia': '突尼斯',
    'Belgium': '比利时', 'Egypt': '埃及', 'Iran': '伊朗', 'New Zealand': '新西兰',
    'Spain': '西班牙', 'Cape Verde': '佛得角', 'Saudi Arabia': '沙特阿拉伯',
    'Uruguay': '乌拉圭', 'France': '法国', 'Norway': '挪威', 'Senegal': '塞内加尔',
    'Iraq': '伊拉克', 'Argentina': '阿根廷', 'Algeria': '阿尔及利亚', 'Austria': '奥地利',
    'Jordan': '约旦', 'Portugal': '葡萄牙',
    'Democratic Republic of the Congo': '民主刚果', 'DR Congo': '民主刚果', 'Congo DR': '民主刚果',
    'Uzbekistan': '乌兹别克斯坦', 'Colombia': '哥伦比亚', 'England': '英格兰',
    'Croatia': '克罗地亚', 'Ghana': '加纳', 'Panama': '巴拿马',
}

def get_cn(name):
    return NAME_MAP.get(name, name)

# Load games data
cache_file = ROOT / 'data' / 'api_cache' / 'wc26__get_games.json'
with open(cache_file) as f:
    data = json.load(f)

games = data.get('games', [])
print(f'Loaded {len(games)} games from cache')

# Connect to DB
db_path = ROOT / 'data' / 'football_data.db'
db = sqlite3.connect(str(db_path))
db.execute('PRAGMA journal_mode=WAL')

# Delete existing WC matches
existing = db.execute('SELECT COUNT(*) FROM matches WHERE league_id=6').fetchone()[0]
print(f'Existing WC matches: {existing}')
if existing > 0:
    db.execute('DELETE FROM matches WHERE league_id=6')
    print(f'Deleted {existing} old WC matches')

imported = 0
skipped = 0

for g in games:
    home_en = g.get('home_team_name_en', '')
    away_en = g.get('away_team_name_en', '')
    home_cn = get_cn(home_en)
    away_cn = get_cn(away_en)
    
    hs = g.get('home_score')
    as_ = g.get('away_score')
    
    # Skip future matches (no real scores)
    finished = str(g.get('finished', '')).upper()
    if finished != 'TRUE':
        skipped += 1
        continue
    
    # Handle null scores for finished matches
    if hs is None or as_ is None:
        hs = 0
        as_ = 0
    
    # Parse date
    date_str = g.get('local_date', '')
    match_time = None
    try:
        dt = datetime.strptime(date_str.strip(), '%m/%d/%Y %H:%M')
        match_date = dt.strftime('%Y-%m-%d')
        match_time = dt.strftime('%H:%M:%S')
    except:
        match_date = date_str[:10] if date_str else '2026-06-01'
        match_time = '00:00:00'
    
    matchday = int(g.get('matchday', 1))
    group = g.get('group', '')
    
    # Determine stage
    if matchday >= 4:
        stage_name = f'世界杯 R32'
    elif matchday <= 3:
        stage_name = f'世界杯 小组赛 {group}'
    else:
        stage_name = '世界杯'
    
    try:
        db.execute('''
            INSERT INTO matches (match_date, match_time, league_id, league_name,
                home_team_id, away_team_id, home_team_name, away_team_name, 
                home_score, away_score,
                status, matchday, halftime_home, halftime_away, final_result)
            VALUES (?, ?, 6, ?, 0, 0, ?, ?, ?, ?, 'finished', ?, NULL, NULL, ?)
        ''', (
            match_date, match_time, stage_name,
            home_cn, away_cn, int(hs), int(as_),
            matchday,
            'H' if int(hs) > int(as_) else ('D' if int(hs) == int(as_) else 'A')
        ))
        imported += 1
    except Exception as e:
        print(f'  ERROR importing {home_cn} vs {away_cn}: {e}')

db.commit()

# Verify
count = db.execute('SELECT COUNT(*) FROM matches WHERE league_id=6').fetchone()[0]
finished_count = db.execute('SELECT COUNT(*) FROM matches WHERE league_id=6 AND status="finished" AND home_score IS NOT NULL').fetchone()[0]
print(f'\nImported: {imported} | Skipped (future): {skipped}')
print(f'WC matches in DB: {count} | With scores: {finished_count}')

# Show latest
rows = db.execute('''
    SELECT match_date, home_team_name, away_team_name, home_score, away_score, league_name
    FROM matches WHERE league_id=6 ORDER BY match_date DESC LIMIT 10
''').fetchall()
print('\nLatest WC matches:')
for r in rows:
    print(f'  {r[0]} | {r[1]} {r[3]}-{r[4]} {r[2]} | {r[5]}')

db.close()
print('\nDone!')
