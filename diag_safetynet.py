import sqlite3, time
from datetime import datetime, timezone, timedelta

DB = "D:/Architecture/data/events.db"
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
now_s = time.time()

rows2 = con.execute("""
    SELECT match_key, home, away, league, kickoff, score_home, score_away, minute
    FROM matches
    WHERE status = 'scheduled' AND kickoff IS NOT NULL AND kickoff != ''
""").fetchall()
print(f"rows2 (scheduled w/ kickoff): {len(rows2)}")
live_would = 0; fin_would = 0; leave = 0
for row in rows2:
    try:
        kt = datetime.strptime(row["kickoff"][:16], "%Y-%m-%d %H:%M")
        kt_utc = kt.replace(tzinfo=timezone(timedelta(hours=8)))
        age = now_s - kt_utc.timestamp()
    except Exception as e:
        print("  parse fail", row["match_key"], e); continue
    home_ok = bool(row["home"] and str(row["home"]).strip())
    away_ok = bool(row["away"] and str(row["away"]).strip())
    if age <= 2 * 60:
        decision = "LEAVE(future/recent)"
        leave += 1
    elif age < 2.25 * 3600:
        decision = f"LIVE (home_ok={home_ok},away_ok={away_ok})"
        live_would += 1
    else:
        decision = "FINISHED"
        fin_would += 1
    print(f"  age={age/60:6.1f}min  {decision:30}  {row['match_key'][:30]}")
print(f"\n→ would flip LIVE={live_would}  FINISHED={fin_would}  LEAVE={leave}")
con.close()
