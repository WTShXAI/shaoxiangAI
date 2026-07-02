#!/usr/bin/env python3
"""导入历史比赛数据到 SQLite — 直接SQL版本 (2026-07-01)"""
import sys, os, json, sqlite3

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')

def import_backtest_matches():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, 'data', 'full_backtest_72_matches.json')
    with open(path, encoding='utf-8') as f:
        matches = json.load(f)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # 确保表存在
    cur.execute("SELECT COUNT(*) FROM matches")
    existing = cur.fetchone()[0]

    imported = 0
    for i, m in enumerate(matches):
        home = m.get('home', '')
        away = m.get('away', '')
        date_str = m.get('date', '')
        actual = m.get('actual', '')
        
        # 解析日期
        parts = date_str.split('/')
        match_date = f'2026-{parts[0].zfill(2)}-{parts[1].zfill(2)}' if len(parts) == 2 else '2026-06-01'
        
        # 解析比分
        home_score = away_score = None
        final_result = None
        if actual and '-' in actual:
            try:
                hs, as_ = actual.split('-')
                home_score = int(hs); away_score = int(as_)
                final_result = 'H' if home_score > away_score else ('A' if home_score < away_score else 'D')
            except (ValueError, AttributeError):
                pass

        status = 'finished' if final_result else 'scheduled'

        try:
            cur.execute('''
                INSERT OR IGNORE INTO matches 
                (match_date, match_time, league_id, league_name, home_team_id, home_team_name,
                 away_team_id, away_team_name, home_score, away_score, final_result, status, matchday)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                match_date, '20:00', 2000, '世界杯',
                i * 2 + 1, home,
                i * 2 + 2, away,
                home_score, away_score, final_result, status, i // 4 + 1
            ))
            imported += 1
        except Exception as e:
            print(f"  Skip insert: {e}")

    conn.commit()

    # Mark 5 as scheduled for future
    cur.execute("""
        UPDATE matches SET status='scheduled', match_date='2026-07-15', home_score=NULL, away_score=NULL, final_result=NULL
        WHERE match_id IN (SELECT match_id FROM matches ORDER BY match_id LIMIT 5)
    """)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM matches")
    total = cur.fetchone()[0]
    conn.close()

    print(f'✅ Imported {imported}/72 matches')
    print(f'   DB total: {total} matches')
    print(f'   Scheduled: 5 (test)')

if __name__ == '__main__':
    import_backtest_matches()
