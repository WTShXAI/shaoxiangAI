import sqlite3, datetime
c = sqlite3.connect("D:/Architecture/data/events.db")
cur = c.cursor()
now = datetime.datetime.now(); now_s = now.timestamp()
# scheduled 且 kickoff 在过去2分钟以上, 统计 + 看 mid 是否 NULL
cur.execute("""SELECT COUNT(*),
  SUM(CASE WHEN mid IS NULL OR mid='' THEN 1 ELSE 0 END),
  SUM(CASE WHEN kickoff IS NULL OR kickoff='' THEN 1 ELSE 0 END)
  FROM matches WHERE status='scheduled'""")
tot, nullmid, nullko = cur.fetchone()
print("scheduled 总数=", tot, " 其中 mid NULL=", nullmid, " kickoff NULL=", nullko)
cur.execute("SELECT COUNT(*) FROM matches WHERE status='scheduled' AND kickoff IS NOT NULL AND kickoff < ?",
            ((now - datetime.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),))
print("scheduled 且 kickoff 早于 (now-2min) 的场数 =", cur.fetchone()[0])
print("\n样本(前10, 含 mid/kickoff):")
cur.execute("SELECT match_key, kickoff, mid, last_seen FROM matches WHERE status='scheduled' AND kickoff IS NOT NULL ORDER BY kickoff DESC LIMIT 10")
for r in cur.fetchall():
    print("  ", r[1], "| mid=", r[2], "|", r[0])
c.close()
