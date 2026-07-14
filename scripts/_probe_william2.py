import sqlite3, json
from collections import Counter
con = sqlite3.connect(r'D:\Architecture\data\football_data.db')
cur = con.cursor()

# 1) label 编码含义: 交叉 h_ft/a_ft 与 label
print('=== label 编码 (h_ft,a_ft -> label) ===')
cur.execute('SELECT h_ft, a_ft, label, COUNT(*) FROM william_ht GROUP BY h_ft, a_ft, label ORDER BY 2 DESC LIMIT 12')
for r in cur.fetchall():
    print('  h_ft=%s a_ft=%s label=%s n=%s' % r)

# 2) 五大联赛 league_name 精确值
print('\n=== league_name 去重分布 (strip后) ===')
cur.execute('SELECT TRIM(league_name), COUNT(*) FROM william_ht GROUP BY TRIM(league_name) ORDER BY 2 DESC')
all_l = cur.fetchall()
for name, n in all_l[:40]:
    print('  %-20s %d' % (name, n))
# 命中五大联赛
targets = ['英超', '西甲', '意甲', '德甲', '法甲', '超级', '甲', '英', '西', '意', '德', '法']
print('\n=== 五大联赛候选匹配 ===')
for name, n in all_l:
    if any(t in name for t in ['英超', '西甲', '意甲', '德甲', '法甲']):
        print('  MATCH %-20s %d' % (name, n))

# 3) 日期范围
print('\n=== 日期范围 ===')
cur.execute('SELECT MIN(match_date), MAX(match_date) FROM william_ht')
print('  ', cur.fetchone())

# 4) wc_xlsx_matches 时间列
print('\n=== wc_xlsx_matches 时间/轮次列 ===')
cur.execute("PRAGMA table_info(wc_xlsx_matches)")
wc_cols = [r[1] for r in cur.fetchall()]
print('  cols:', wc_cols)
for c in wc_cols:
    if any(k in c.lower() for k in ['date', 'time', 'round', 'md', 'stage', 'group', 'matchday', 'order']):
        print('   time-ish col:', c)
cur.execute('SELECT edition, COUNT(*) FROM wc_xlsx_matches GROUP BY edition')
print('  editions:', cur.fetchall())
