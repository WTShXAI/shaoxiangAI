import json, re

raw = open("leyu_settled_all.json", encoding="utf-8").read().strip()
# unwrap possible double-encoding
try:
    data = json.loads(raw)
except Exception:
    data = json.loads(json.loads(raw))
if isinstance(data, str):
    data = json.loads(data)

print("total field:", data.get("total"), "amt:", data.get("amt"), "win:", data.get("win"))
print("pages collected (rows):", data.get("pages"), "actual rows:", len(data["rows"]))
rows = data["rows"]
dates = []
cs = []
for r in rows:
    if len(r) < 8:
        print("SHORT ROW:", r); continue
    detail = r[3]
    m = re.search(r"(\d{4}-\d{2}-\d{2})", r[1])
    dates.append(m.group(1) if m else "?")
    if "波胆" in detail:
        sc = re.search(r"(\d+-\d+)\s*@([\d.]+)", detail)
        res = "赢" if "赢" in detail else ("输" if "输" in detail else "?")
        cs.append((r[1][:22], sc.group(1) if sc else "?", sc.group(2) if sc else "?", r[4], r[5], r[6], res))

dates = [d for d in dates if d != "?"]
print("date range:", min(dates), "->", max(dates))
print("CS (波胆) bets in collected:", len(cs))
for c in cs:
    print(c)
