"""
G9 单测 — 自包含临时 DB, 不依赖 452MB 库 / 外部 key (CI 可移植)
验证: 6场赔率幂等 upsert 到 odds + odds_features 同步 + phantom 警告 + 非目标跳过
"""
import os, json, sqlite3, sys, subprocess

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'g9_fill_wc6.py')
PY = sys.executable


def _chk(name, cond):
    print(('PASS' if cond else 'FAIL') + ' ' + name)
    assert cond, name


def _build_tmp_db(path):
    con = sqlite3.connect(path)
    c = con.cursor()
    c.executescript('''
    CREATE TABLE matches (match_id INTEGER, league_name TEXT, match_date TEXT, home_team_name TEXT, away_team_name TEXT);
    CREATE TABLE odds (match_id INTEGER, provider TEXT, home_odds REAL, draw_odds REAL, away_odds REAL, return_rate REAL, odds_timestamp TEXT, created_at TEXT);
    CREATE TABLE odds_features (home_team TEXT, away_team TEXT, match_date TEXT, open_h REAL, open_d REAL, open_a REAL, close_h REAL, close_d REAL, close_a REAL);
    ''')
    rows = [
        (537375, '世界杯', '2026-07-04', 'Paraguay', 'France'),
        (537376, '世界杯', '2026-07-04', 'Canada', 'Morocco'),
        (537377, '世界杯', '2026-07-05', 'Brazil', 'Norway'),
        (537378, '世界杯', '2026-07-06', 'Mexico', 'England'),
        (2130600, '世界杯', '2026-07-06', 'Portugal', 'Spain'),
        (537380, '世界杯', '2026-07-07', 'USA', 'Belgium'),
        (999999, '世界杯', '2026-07-07', 'Other', 'Team'),  # 非目标
    ]
    c.executemany('INSERT INTO matches VALUES (?,?,?,?,?)', rows)
    con.commit()
    con.close()


def test_g9_upsert_and_phantom(tmp_path):
    db = str(tmp_path / 'wc.db')
    _build_tmp_db(db)
    recs = [
        {"match_id": 537375, "home_odds": 1.4, "draw_odds": 5.0, "away_odds": 11.6, "return_rate": 0.95},
        {"match_id": 537376, "home_odds": 3.7, "draw_odds": 3.2, "away_odds": 2.3},
        {"match_id": 537380, "home_odds": 4.2, "draw_odds": 3.8, "away_odds": 1.9},  # phantom
        {"match_id": 999999, "home_odds": 2.0, "draw_odds": 3.0, "away_odds": 4.0},  # 非目标跳过
    ]
    jf = tmp_path / 'wc6.json'
    json.dump(recs, open(jf, 'w'))
    out = subprocess.run([PY, SCRIPT, '--json', str(jf), '--db', db], capture_output=True, text=True)
    _chk('G9-1 退出码 0', out.returncode == 0)
    _chk('G9-2 phantom 警告出现', 'phantom' in out.stdout.lower())
    con = sqlite3.connect(db)
    c = con.cursor()
    n_odds = c.execute("SELECT COUNT(*) FROM odds WHERE provider='manual'").fetchone()[0]
    _chk('G9-3 odds 表入库 3 条(非目标跳过)', n_odds == 3)
    bad = c.execute("SELECT COUNT(*) FROM odds WHERE match_id=999999").fetchone()[0]
    _chk('G9-4 非目标未入库', bad == 0)
    of = c.execute("SELECT close_h, close_d, close_a FROM odds_features WHERE home_team='Paraguay' AND away_team='France'").fetchone()
    _chk('G9-5 odds_features 同步 close', of is not None and abs(of[0] - 1.4) < 1e-6)
    # 幂等: 再跑一次仍 3 条
    subprocess.run([PY, SCRIPT, '--json', str(jf), '--db', db], capture_output=True, text=True)
    n2 = c.execute("SELECT COUNT(*) FROM odds WHERE provider='manual'").fetchone()[0]
    _chk('G9-6 幂等(重复跑仍 3 条)', n2 == 3)
    con.close()


if __name__ == '__main__':
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    test_g9_upsert_and_phantom(d)
    print('\n=== G9 单测全部 PASS ===')
