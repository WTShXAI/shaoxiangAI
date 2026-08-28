import sqlite3, time
from datetime import datetime, timezone, timedelta

DB = "D:/Architecture/data/events.db"
con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
now_s = time.time()

rows = con.execute("SELECT status, COUNT(*) c FROM matches GROUP BY status").fetchall()
print("STATUS:", {r['status']: r['c'] for r in rows})

sched = con.execute(
    "SELECT match_key, kickoff FROM matches WHERE status='scheduled' AND kickoff IS NOT NULL AND kickoff != ''"
).fetchall()
orphan = 0; inwindow = 0; future = 0; samples_inwindow = []
for r in sched:
    try:
        kt = datetime.strptime(r['kickoff'][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8)))
        age = now_s - kt.timestamp()
    except Exception:
        continue
    if age > 2.25 * 3600:
        orphan += 1
    elif age > 120:
        inwindow += 1
        if len(samples_inwindow) < 5:
            samples_inwindow.append((r['match_key'][:30], r['kickoff'], round(age/60,1)))
    else:
        future += 1

print(f"scheduled 总数: {len(sched)}")
print(f"  真孤儿 (age>2.25h, 应=0): {orphan}")
print(f"  在窗 (2min<age<2.25h, 待翻live): {inwindow}")
print(f"  未来 (age<=2min, 真未开赛): {future}")
for s in samples_inwindow:
    print("    inwindow:", s)
con.close()
