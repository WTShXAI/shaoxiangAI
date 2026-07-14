import sqlite3
from datetime import datetime, timedelta, timezone

import bridge_service


def test_load_real_match_data_prefers_live_odds_rows(tmp_path):
    db_path = tmp_path / "football_data.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE live_odds_raw (
            id INTEGER PRIMARY KEY,
            sport_key TEXT,
            home_team TEXT,
            away_team TEXT,
            home_team_en TEXT,
            away_team_en TEXT,
            commence_time TEXT,
            best_h2h TEXT,
            bookmaker_prob TEXT,
            handicap_markets TEXT,
            bookmakers_detail TEXT,
            captured_at TEXT
        )
        """
    )
    cur.execute(
        """
        INSERT INTO live_odds_raw (
            sport_key, home_team, away_team, home_team_en, away_team_en,
            commence_time, best_h2h, bookmaker_prob, handicap_markets,
            bookmakers_detail, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "soccer_fifa_world_cup",
            "Alpha",
            "Beta",
            "Alpha",
            "Beta",
            (datetime.now(timezone.utc) + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            '{"home": 1.65, "draw": 3.8, "away": 5.0}',
            '{}',
            '{}',
            '[{"name": "pinnacle"}, {"name": "betfair"}]',
            '2026-07-10T21:46:46.273050+08:00',
        ),
    )
    conn.commit()
    conn.close()

    fixtures, matches, leagues = bridge_service._load_real_match_data(db_path=str(db_path), days=7)

    assert fixtures[0]["home"] == "Alpha"
    assert fixtures[0]["away"] == "Beta"
    assert fixtures[0]["odds_h"] == 1.65
    assert matches[0]["homeTeam"]["name"] == "Alpha"
    assert leagues[0]["code"] == "WC26"
