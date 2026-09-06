"""
P0-② 对齐验证 — 用 betting_markets 的大小球赔率(庄家sharp)交叉验证
ou_goals_scores.json 里 68 场 (match_id -> 真实比分) 的映射是否对齐.

逻辑:
  - 每场取 OU 2.5 的 over_odds / under_odds.
  - 庄家"偏好侧" = over/under 中较低赔率那一侧.
  - 真实侧 = 总进球>2.5 为 over, 否则 under.
  - 若映射对齐, 庄家偏好侧应大部分命中(赔率是 sharp 的), 且 over_odds 与总进球应呈负相关.
  - 同时用 1X2 赔率做第二重交叉: 庄家 favorite 的赛果方向应与真实赛果一致.
"""
import json, os, sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(ROOT, "deliverables", "ou_goals_scores.json")
DB = os.path.join(ROOT, "data", "football_data.db")

data = json.load(open(JSON_PATH, encoding="utf-8"))
scores = data["scores"]
con = sqlite3.connect(DB)

rows = []
for mid, sc in scores.items():
    mid_i = int(mid)
    tg = sc["hs"] + sc["as"]
    over = con.execute(
        "SELECT odds FROM betting_markets WHERE match_id=? AND market_type='totals' AND market_line='2.5' AND outcome_name='over'",
        (mid_i,)).fetchone()
    under = con.execute(
        "SELECT odds FROM betting_markets WHERE match_id=? AND market_type='totals' AND market_line='2.5' AND outcome_name='under'",
        (mid_i,)).fetchone()
    h1 = con.execute(
        "SELECT odds FROM betting_markets WHERE match_id=? AND market_type='1x2' AND outcome_name='home'",
        (mid_i,)).fetchone()
    d1 = con.execute(
        "SELECT odds FROM betting_markets WHERE match_id=? AND market_type='1x2' AND outcome_name='draw'",
        (mid_i,)).fetchone()
    a1 = con.execute(
        "SELECT odds FROM betting_markets WHERE match_id=? AND market_type='1x2' AND outcome_name='away'",
        (mid_i,)).fetchone()
    rows.append({
        "mid": mid_i, "tg": tg, "hs": sc["hs"], "as": sc["as"],
        "over": over[0] if over else None, "under": under[0] if under else None,
        "h1": h1[0] if h1 else None, "d1": d1[0] if d1 else None, "a1": a1[0] if a1 else None,
        "home_en": sc.get("home_en"), "away_en": sc.get("away_en"),
    })

# ---- 大小球对齐检验 ----
valid = [r for r in rows if r["over"] and r["under"]]
fav_hits = 0
for r in valid:
    market_side = "over" if r["over"] < r["under"] else "under"
    actual_side = "over" if r["tg"] > 2.5 else "under"
    if market_side == actual_side:
        fav_hits += 1
acc = fav_hits / len(valid) if valid else 0

# 相关性: over_odds vs 总进球 (Pearson 近似)
import statistics
overs = [r["over"] for r in valid]
tgs = [r["tg"] for r in valid]
n = len(overs)
if n > 1:
    mo = statistics.mean(overs); mt = statistics.mean(tgs)
    cov = sum((o-mo)*(t-mt) for o,t in zip(overs,tgs))
    so = statistics.pstdev(overs); st = statistics.pstdev(tgs)
    corr = cov/(so*st) if so*st else 0
else:
    corr = 0

print(f"[大小球对齐] 有赔率场次={len(valid)}/68  庄家偏好侧命中率={acc:.3f}")
print(f"[大小球对齐] over_odds 与 总进球 Pearson 相关={corr:.3f} (预期显著为负: 低水=大球)")

# ---- 1X2 交叉检验 ----
v2 = [r for r in rows if r["h1"] and r["d1"] and r["a1"]]
hits = 0
for r in v2:
    fav = min(("H",r["h1"]),("D",r["d1"]),("A",r["a1"]), key=lambda x:x[1])[0]
    if r["hs"]>r["as"]: res="H"
    elif r["hs"]<r["as"]: res="A"
    else: res="D"
    if fav==res: hits+=1
acc2 = hits/len(v2) if v2 else 0
print(f"[1X2交叉]   有赔率场次={len(v2)}/68  庄家favorite命中率={acc2:.3f} (随机基线~33%, sharp应>45%)")

# ---- 列出强矛盾(大小球) ----
print("\n强矛盾清单 (庄家偏好侧未命中, 用于人工抽查):")
for r in valid:
    market_side = "over" if r["over"] < r["under"] else "under"
    actual_side = "over" if r["tg"] > 2.5 else "under"
    if market_side != actual_side:
        print(f"  {r['mid']} {r['home_en']}-{r['away_en']} {r['hs']}-{r['as']} (tg={r['tg']}) 庄家偏好={market_side}(o={r['over']}/u={r['under']}) 实际={actual_side}")

con.close()
