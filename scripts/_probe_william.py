import sqlite3, json
con = sqlite3.connect(r'D:\Architecture\data\football_data.db')
cur = con.cursor()
cur.execute("PRAGMA table_info(william_ht)")
cols = [r[1] for r in cur.fetchall()]
print('WILLIAM_HT COLS:', cols)
cur.execute('SELECT COUNT(*) FROM william_ht')
print('WILLIAM_HT ROWS:', cur.fetchone()[0])
sample = cur.execute('SELECT * FROM william_ht LIMIT 1')
names = [d[0] for d in cur.description]
print('SAMPLE:', json.dumps(dict(zip(names, sample.fetchone())), ensure_ascii=False)[:600])
for lc in ['league', 'division', 'country', 'competition']:
    if lc in cols:
        cur.execute(f'SELECT {lc}, COUNT(*) FROM william_ht GROUP BY {lc} ORDER BY 2 DESC LIMIT 15')
        print(f'DIST by {lc}:', cur.fetchall())
# also check date / odds col names hints
print('--- col name hints ---')
for c in cols:
    if any(k in c.lower() for k in ['date', 'time', 'home', 'away', 'h_', 'a_', 'o', 'goal', 'score', 'hg', 'ag', 'result']):
        print('  ', c)
