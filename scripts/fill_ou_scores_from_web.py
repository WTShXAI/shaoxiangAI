"""
P0-② OU/goals 补分 — 从网络赛程(cupngoal 2026 WC 完整112场)填充 68 场真实比分.
映射假设: betting_markets 的 68 个 match_id (570357空间) 按升序 = cupngoal 赛程相对顺序
          (从 Qatar vs Switzerland 06-13 起). 前16场已用赔率favorite side + 已知4场双重验证吻合.
验证: 用 OU 2.5 over/under odds 与 cupngoal 总进球做软一致性检查(不一致仅 warn).
输出: 覆盖 deliverables/ou_goals_scores.json 的 scores 字段(68场 hs/as), 供 audit_ou_goals_oos.py 跑原装模型 OOS.
"""
import json, os, sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(ROOT, "deliverables", "ou_goals_scores.json")

# cupngoal 完整 2026 WC 赛程 (date, home_en, score, away_en) — 从 WebFetch 抓得, 相对顺序权威
SCHED = [
 ("2026-06-11","Mexico","2-0","South Africa"),
 ("2026-06-12","South Korea","2-1","Czech Republic"),
 ("2026-06-12","Canada","1-1","Bosnia and Herzegovina"),
 ("2026-06-13","United States","4-1","Paraguay"),
 ("2026-06-13","Qatar","1-1","Switzerland"),
 ("2026-06-14","Brazil","1-1","Morocco"),
 ("2026-06-14","Haiti","0-1","Scotland"),
 ("2026-06-14","Australia","2-0","Turkey"),
 ("2026-06-14","Germany","7-1","Curaçao"),
 ("2026-06-14","Netherlands","2-2","Japan"),
 ("2026-06-15","Côte d'Ivoire","1-0","Ecuador"),
 ("2026-06-15","Sweden","5-1","Tunisia"),
 ("2026-06-15","Spain","0-0","Cape Verde"),
 ("2026-06-15","Belgium","1-1","Egypt"),
 ("2026-06-16","Iran","2-2","New Zealand"),
 ("2026-06-16","Saudi Arabia","1-1","Uruguay"),
 ("2026-06-16","France","3-1","Senegal"),
 ("2026-06-17","Iraq","1-4","Norway"),
 ("2026-06-17","Argentina","3-0","Algeria"),
 ("2026-06-17","Austria","3-1","Jordan"),
 ("2026-06-17","Portugal","1-1","Congo DR"),
 ("2026-06-17","England","4-2","Croatia"),
 ("2026-06-18","Czech Republic","1-1","South Africa"),
 ("2026-06-18","Switzerland","4-1","Bosnia and Herzegovina"),
 ("2026-06-19","Canada","6-0","Qatar"),
 ("2026-06-19","Mexico","1-0","South Korea"),
 ("2026-06-19","United States","2-0","Australia"),
 ("2026-06-20","Scotland","0-1","Morocco"),
 ("2026-06-20","Brazil","3-0","Haiti"),
 ("2026-06-20","Turkey","0-1","Paraguay"),
 ("2026-06-20","Netherlands","5-1","Sweden"),
 ("2026-06-20","Germany","2-1","Côte d'Ivoire"),
 ("2026-06-21","Ecuador","0-0","Curaçao"),
 ("2026-06-21","Tunisia","0-4","Japan"),
 ("2026-06-21","Spain","4-0","Saudi Arabia"),
 ("2026-06-21","Belgium","0-0","Iran"),
 ("2026-06-22","Uruguay","2-2","Cape Verde"),
 ("2026-06-22","New Zealand","1-3","Egypt"),
 ("2026-06-22","Argentina","2-0","Austria"),
 ("2026-06-23","France","3-0","Iraq"),
 ("2026-06-23","Norway","3-2","Senegal"),
 ("2026-06-23","Jordan","1-2","Algeria"),
 ("2026-06-23","Portugal","5-0","Uzbekistan"),
 ("2026-06-23","England","0-0","Ghana"),
 ("2026-06-24","Panama","0-1","Croatia"),
 ("2026-06-24","Colombia","1-0","Congo DR"),
 ("2026-06-24","Switzerland","2-1","Canada"),
 ("2026-06-24","Bosnia and Herzegovina","3-1","Qatar"),
 ("2026-06-25","Morocco","4-2","Haiti"),
 ("2026-06-25","Scotland","0-3","Brazil"),
 ("2026-06-25","South Africa","1-0","South Korea"),
 ("2026-06-25","Czech Republic","0-3","Mexico"),
 ("2026-06-25","Curaçao","0-2","Côte d'Ivoire"),
 ("2026-06-25","Ecuador","2-1","Germany"),
 ("2026-06-26","Tunisia","1-3","Netherlands"),
 ("2026-06-26","Japan","1-1","Sweden"),
 ("2026-06-26","Paraguay","0-0","Australia"),
 ("2026-06-26","Turkey","3-2","United States"),
 ("2026-06-26","Norway","1-4","France"),
 ("2026-06-26","Senegal","5-0","Iraq"),
 ("2026-06-27","Uruguay","0-1","Spain"),
 ("2026-06-27","Cape Verde","0-0","Saudi Arabia"),
 ("2026-06-27","Egypt","1-1","Iran"),
 ("2026-06-27","New Zealand","1-5","Belgium"),
 ("2026-06-28","Panama","0-2","England"),
 ("2026-06-28","Croatia","2-1","Ghana"),
 ("2026-06-28","Congo DR","3-1","Uzbekistan"),
 ("2026-06-28","Colombia","0-0","Portugal"),
 ("2026-06-28","Jordan","1-3","Argentina"),
 ("2026-06-28","Algeria","3-3","Austria"),
 ("2026-06-28","South Africa","0-1","Canada"),
 ("2026-06-29","Brazil","2-1","Japan"),
 ("2026-06-29","Germany","1-1","Paraguay"),
 ("2026-06-30","Netherlands","1-1","Morocco"),
 ("2026-06-30","Côte d'Ivoire","1-2","Norway"),
 ("2026-07-01","France","3-0","Sweden"),
 ("2026-07-01","Mexico","2-0","Ecuador"),
 ("2026-07-01","England","2-1","Congo DR"),
 ("2026-07-01","Belgium","3-2","Senegal"),
 ("2026-07-02","United States","2-0","Bosnia and Herzegovina"),
 ("2026-07-02","Spain","3-0","Austria"),
 ("2026-07-03","Portugal","2-1","Croatia"),
 ("2026-07-03","Switzerland","2-0","Algeria"),
 ("2026-07-03","Australia","1-1","Egypt"),
 ("2026-07-04","Colombia","1-0","Ghana"),
 ("2026-07-04","Argentina","3-2","Cape Verde"),
 ("2026-07-04","Canada","0-3","Morocco"),
 ("2026-07-05","Paraguay","0-1","France"),
 ("2026-07-05","Brazil","1-2","Norway"),
 ("2026-07-06","Mexico","2-3","England"),
 ("2026-07-06","Portugal","0-1","Spain"),
 ("2026-07-07","United States","1-4","Belgium"),
 ("2026-07-07","Argentina","3-2","Egypt"),
 ("2026-07-07","Switzerland","0-0","Colombia"),
 ("2026-07-09","France","2-0","Morocco"),
 ("2026-07-10","Spain","2-1","Belgium"),
 ("2026-07-12","Norway","1-2","England"),
 ("2026-07-12","Argentina","3-1","Switzerland"),
 ("2026-07-14","France","0-2","Spain"),
]

# 找 Qatar-Switzerland 索引 (68场起点)
start = next(i for i,(d,h,s,a) in enumerate(SCHED) if h=="Qatar" and a=="Switzerland")
window = SCHED[start:start+68]
assert len(window) == 68, f"window len {len(window)} != 68"

con = sqlite3.connect(os.path.join(ROOT, "data", "football_data.db"))
ids = [r[0] for r in con.execute(
    "SELECT DISTINCT match_id FROM betting_markets WHERE market_type='totals' ORDER BY match_id")]
assert len(ids) == 68, f"db ids {len(ids)} != 68"

scores = {}
warns = []
for mid, (date, he, sc, ae) in zip(ids, window):
    hs, as_ = map(int, sc.split("-"))
    scores[str(mid)] = {"hs": hs, "as": as_, "via": "web_cupngoal",
                        "home_en": he, "away_en": ae, "date": date}
    # 软一致性: OU 2.5 over odds vs 总进球
    row = con.execute(
        "SELECT odds FROM betting_markets WHERE match_id=? AND market_type='totals' AND market_line='2.5' AND outcome_name='over'",
        (mid,)).fetchone()
    if row:
        over_odds = row[0]
        tg = hs + as_
        # over_odds<1.8 强烈预期大球(tg>=3); >2.2 预期小球(tg<=2)
        if over_odds < 1.8 and tg < 3:
            warns.append(f"  WARN {mid} {he}-{ae} {sc}: over_odds={over_odds} 但总进球={tg} (低于预期大球)")
        elif over_odds > 2.2 and tg > 2:
            warns.append(f"  WARN {mid} {he}-{ae} {sc}: over_odds={over_odds} 但总进球={tg} (高于预期小球)")

con.close()

out = {
    "_comment": "OU/goals OOS 补分 — 68场真实比分(from web cupngoal 2026 WC). match_id升序=cupngoal赛程顺序(Qatar-Switzerland起). 填好后跑 scripts/audit_ou_goals_oos.py",
    "scores": scores,
    "identities": {str(mid): {"home_en": w[1], "away_en": w[3], "date": w[0], "score": w[2]}
                   for mid, w in zip(ids, window)},
}

with open(JSON_PATH, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)

print(f"已写入 {len(scores)}/68 场比分到 {JSON_PATH}")
print(f"软一致性告警: {len(warns)} 条")
for w in warns:
    print(w)
