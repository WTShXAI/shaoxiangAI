import sqlite3
c = sqlite3.connect('data/football_data.db')
print('=== wc_all_matches 赔率/比分完整性 ===')
rows = c.execute(
    "select edition,count(*),"
    "sum(case when oh is null or od is null or oa is null then 1 else 0 end),"
    "sum(case when hg is null or ag is null then 1 else 0 end) "
    "from wc_all_matches group by edition"
).fetchall()
for e in rows:
    print(f"  {e[0]}: total={e[1]}, null_odds={e[2]}, null_score={e[3]}")

print('=== teams attack/defense 覆盖 ===')
n = c.execute(
    "select count(*) from teams where attack_strength is not null and defense_strength is not null"
).fetchone()[0]
print('  teams with attack/defense:', n)

print('=== 主odds表 WC2026 缺赔率场次 ===')
miss = c.execute(
    "select count(*) from matches m where m.league_name like '%World Cup%' "
    "and m.status='FINISHED' and m.match_id not in "
    "(select distinct match_id from odds where match_id is not null)"
).fetchone()[0]
print('  WC matches missing in odds table:', miss)

print('=== wc_all_matches 样例 ===')
for r in c.execute("select edition,home,away,hg,ag,oh,od,oa from wc_all_matches limit 5"):
    print('  ', r)
