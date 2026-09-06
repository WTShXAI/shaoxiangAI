import sqlite3, time
from datetime import datetime, timezone, timedelta

DB = "D:/Architecture/data/events.db"
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
now_s = time.time()

rows = con.execute("SELECT status, COUNT(*) c FROM matches GROUP BY status").fetchall()
print("STATUS:", {r['status']: r['c'] for r in rows})

# STUCK: scheduled with kickoff in past (>2min ago)
stuck = con.execute(
    "SELECT match_key, mid, kickoff FROM matches "
    "WHERE status='scheduled' AND kickoff IS NOT NULL AND kickoff != ''"
).fetchall()
cnt = 0; samples = []
for r in stuck:
    try:
        kt = datetime.strptime(r['kickoff'][:16], "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone(timedelta(hours=8)))
        age = now_s - kt.timestamp()
    except Exception:
        age = None
    if age is not None and age > 120:
        cnt += 1
        if len(samples) < 6:
            samples.append((r['match_key'], r['kickoff'], round(age/60, 1)))
print("STUCK (scheduled, kickoff >2min ago):", cnt)
for s in samples:
    print("   ", s)

# also: live with kickoff in future (should be 0)
fut = con.execute(
    "SELECT COUNT(*) c FROM matches WHERE status='live' AND kickoff IS NOT NULL AND kickoff != ''"
).fetchall()
print("LIVE total:", fut[0]['c'])
con.close()
