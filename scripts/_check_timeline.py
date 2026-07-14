import sqlite3
c = sqlite3.connect(r'D:\Architecture\data\wc2026_timeline.db')
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("TABLES:", tabs)
for t in tabs:
    try:
        n = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("  %s -> %d rows" % (t, n))
    except Exception as e:
        print("  %s -> err %s" % (t, e))
