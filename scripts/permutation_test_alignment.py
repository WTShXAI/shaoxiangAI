"""
P0-② 对齐决定性检验 — 置换检验(permutation test).

假设我们的 cupngoal 顺序赋值把真实比分正确放到了 570357 空间的 match_id 上,
则 赔率(独立信号) 应与 该比分 存在结构化关系(强于随机).

方法:
  1. 取 68 场 (match_id -> over_odds/under_odds/h1/d1/a1) 固定.
  2. 实际赋值下计算信号指标 M = OU庄家偏好侧命中率 + 1X2 favorite命中率.
  3. 对 (hs,as) 做 2000 次随机置换(比分在 match_id 间打乱, 赔率不动), 每次算 M.
  4. p = (置换中 M>=实际M 的次数 +1) / (N+1). p<0.05 => 实际赋值显著强于随机 => 对齐成立.
"""
import json, os, sqlite3, random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_PATH = os.path.join(ROOT, "deliverables", "ou_goals_scores.json")
DB = os.path.join(ROOT, "data", "football_data.db")

data = json.load(open(JSON_PATH, encoding="utf-8"))
scores = data["scores"]
con = sqlite3.connect(DB)

# 固定赔率
odds = {}
for mid in scores:
    mid_i = int(mid)
    o = con.execute("SELECT odds FROM betting_markets WHERE match_id=? AND market_type='totals' AND market_line='2.5' AND outcome_name='over'",(mid_i,)).fetchone()
    u = con.execute("SELECT odds FROM betting_markets WHERE match_id=? AND market_type='totals' AND market_line='2.5' AND outcome_name='under'",(mid_i,)).fetchone()
    h1 = con.execute("SELECT odds FROM betting_markets WHERE match_id=? AND market_type='1x2' AND outcome_name='home'",(mid_i,)).fetchone()
    d1 = con.execute("SELECT odds FROM betting_markets WHERE match_id=? AND market_type='1x2' AND outcome_name='draw'",(mid_i,)).fetchone()
    a1 = con.execute("SELECT odds FROM betting_markets WHERE match_id=? AND market_type='1x2' AND outcome_name='away'",(mid_i,)).fetchone()
    odds[mid] = (o[0] if o else None, u[0] if u else None,
                 h1[0] if h1 else None, d1[0] if d1 else None, a1[0] if a1 else None)

mids = list(scores.keys())

def signal(pairs):
    """pairs: list of (mid, hs, as_) . 返回 (OU命中率, 1X2命中率, 综合M)."""
    ou_hit = 0; ou_n = 0; x2_hit = 0; x2_n = 0
    for mid, hs, as_ in pairs:
        o, u, h1, d1, a1 = odds[mid]
        tg = hs + as_
        if o and u:
            mside = "over" if o < u else "under"
            aside = "over" if tg > 2.5 else "under"
            ou_n += 1
            if mside == aside: ou_hit += 1
        if h1 and d1 and a1:
            fav = min(("H",h1),("D",d1),("A",a1), key=lambda x:x[1])[0]
            res = "H" if hs>as_ else ("A" if hs<as_ else "D")
            x2_n += 1
            if fav == res: x2_hit += 1
    ou_acc = ou_hit/ou_n if ou_n else 0
    x2_acc = x2_hit/x2_n if x2_n else 0
    return ou_acc, x2_acc, ou_acc + x2_acc

# 实际赋值
actual_pairs = [(mid, scores[mid]["hs"], scores[mid]["as"]) for mid in mids]
ou_a, x2_a, M_a = signal(actual_pairs)

# 置换检验
random.seed(20260715)
N = 2000
count = 0
for _ in range(N):
    perm = mids[:]
    random.shuffle(perm)
    pairs = [(mid, scores[p]["hs"], scores[p]["as"]) for mid, p in zip(mids, perm)]
    _, _, M = signal(pairs)
    if M >= M_a:
        count += 1
p = (count + 1) / (N + 1)

print(f"实际赋值:  OU命中率={ou_a:.3f}  1X2命中率={x2_a:.3f}  综合M={M_a:.3f}")
print(f"置换检验: N={N}  p-value={p:.4f}")
print(f"结论: {'对齐成立(赋值显著强于随机)' if p<0.05 else '对齐不成立(赋值≈随机打乱, 即错位)'}")

# 额外: 真实命中率对比随机期望
print(f"\n参考基线: 随机置换下 OU命中率均值≈{0.5:.2f}, 1X2≈{1/3:.2f}")
con.close()
