"""
直接调 /api/leagues 看 fixture_count 分布
"""
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:9000/api/leagues", timeout=10) as r:
    d = json.loads(r.read())
cats = d.get('data', {}).get('categories', [])
all_lg = []
for c in cats:
    for lg in c.get('leagues', []):
        all_lg.append((lg.get('name'), lg.get('fixture_count'), lg.get('sport_key')))
print(f"total_leagues={len(all_lg)}")
empty = [x for x in all_lg if not x[1] or x[1] == 0]
nonempty = [x for x in all_lg if x[1] and x[1] > 0]
print(f"with_fixtures={len(nonempty)}  empty={len(empty)}")
print("\n=== 0 场联赛样本 (前 15) ===")
for x in empty[:15]:
    print(f"  {x[0]:35s} count={x[1]}")
print("\n=== 有赛程联赛 Top-5 by count ===")
for x in sorted(nonempty, key=lambda y: -y[1])[:5]:
    print(f"  {x[0]:35s} count={x[1]}")
