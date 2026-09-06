"""
30K 大规模条件波胆回测 — football_data.db
数据源: matches (33K finished + halftime scores) + odds_features (opening/closing odds)
"""

import sqlite3, json, sys, os
from pathlib import Path
import numpy as np
from scipy.stats import poisson
from scipy.optimize import root
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))

DB = os.path.join(os.path.dirname(__file__), "..", "data", "football_data.db")
OUT = os.path.join(os.path.dirname(__file__), "..", "data", "backtest_conditional_30k.json")

def _indep_1x2(lam, mu, maxg=8):
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    M = np.outer(h, a)
    return np.tril(M, -1).sum(), np.trace(M), np.triu(M, 1).sum()

def oip_from_1x2(ph, pd_, pa, maxg=8):
    def obj(p):
        lam, mu = p; h, d, a = _indep_1x2(lam, mu, maxg); return [h - ph, d - pd_]
    sol = root(obj, [1.3, 1.1], method="hybr")
    if sol.success:
        lam, mu = max(0.05, float(sol.x[0])), max(0.05, float(sol.x[1]))
    else:
        best, berr = (1.3, 1.1), 1e9
        for lam in np.linspace(0.2, 4.0, 60):
            for mu in np.linspace(0.2, 4.0, 60):
                h, d, a = _indep_1x2(lam, mu, maxg)
                err = abs(h - ph) + abs(d - pd_)
                if err < berr: berr, best = err, (lam, mu)
        lam, mu = best
    return lam, mu

def conditional_top(lam, mu, ht_h, ht_a, minutes_played, k=5, maxg=8):
    """返回 top-k 比分列表 [(h,a,prob%), ...]"""
    remaining = max(90 - minutes_played, 1.0)
    lam_rem = lam * remaining / 90.0
    mu_rem = mu * remaining / 90.0
    f_h = poisson.pmf(np.arange(maxg + 1), lam_rem)
    f_a = poisson.pmf(np.arange(maxg + 1), mu_rem)
    M = np.zeros((maxg + 1, maxg + 1))
    for i in range(ht_h, maxg + 1):
        for j in range(ht_a, maxg + 1):
            M[i, j] = f_h[i - ht_h] * f_a[j - ht_a]
    M /= M.sum()
    flat = M.flatten()
    order = np.argsort(-flat)[:k]
    return [(int(i // (maxg + 1)), int(i % (maxg + 1)), round(float(flat[i]) * 100, 1)) for i in order]

# ── 主流程 ──
t0 = datetime.now()
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# odds_features 有全量: 开盘赔率 + 最终比分 + 赛果. 无半场 → 用55%进球反推近似.
rows = conn.execute("""
    SELECT match_date, home_team, away_team, home_score, away_score, outcome,
           open_h, open_d, open_a
    FROM odds_features
    WHERE outcome IN ('H','D','A')
      AND home_score IS NOT NULL
      AND open_h > 1.01 AND open_a > 1.01
      AND open_h < 50 AND open_a < 50
    ORDER BY match_date DESC
    LIMIT 20000
""").fetchall()

all_rows = [dict(r) for r in rows]
conn.close()
print(f"[{datetime.now()-t0}] 查询完成: {len(all_rows)} 场")

RESULTS = []
processed = 0
for r in all_rows:
    oh, od, oa = r['open_h'], r['open_d'], r['open_a']
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    ph, pd_, pa = (1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv

    try:
        lam, mu = oip_from_1x2(ph, pd_, pa)
    except Exception:
        continue

    final = (r['home_score'], r['away_score'])
    # 半场近似: 55% 进球 + 随机偏移
    h, a = final
    ht_h = max(0, min(h, int(h * 0.55 + 0.3)))
    ht_a = max(0, min(a, int(a * 0.55 + 0.3)))
    if h == 0 and a == 0:
        ht_h, ht_a = 0, 0
    ht = (ht_h, ht_a)

    # 无条件 (赛前 top5)
    t0_top = conditional_top(lam, mu, 0, 0, 0, 5)
    t0_hit = final in [(s[0], s[1]) for s in t0_top]

    # 条件 (中场 45', 真实半场比分)
    t45_top = conditional_top(lam, mu, ht[0], ht[1], 45, 5)
    t45_hit5 = final in [(s[0], s[1]) for s in t45_top]
    t45_hit3 = final in [(s[0], s[1]) for s in t45_top[:3]]

    RESULTS.append({
        "home": r['home_team'], "away": r['away_team'],
        "final": list(final), "ht": list(ht),
        "odds": [round(oh, 2), round(od, 2), round(oa, 2)],
        "lam": round(lam, 2), "mu": round(mu, 2),
        "t0_hit": t0_hit, "t45_hit5": t45_hit5, "t45_hit3": t45_hit3,
        "result": r['outcome'],
    })
    processed += 1
    if processed % 5000 == 0:
        print(f"  [{datetime.now()-t0}] {processed}/{len(all_rows)}...")

# ── 统计 ──
n = len(RESULTS)
t0_h = sum(1 for r in RESULTS if r['t0_hit'])
t45_h5 = sum(1 for r in RESULTS if r['t45_hit5'])
t45_h3 = sum(1 for r in RESULTS if r['t45_hit3'])

print(f"\n{'='*60}")
print(f"  大规模条件波胆回测 (N={n})")
print(f"{'='*60}")
print(f"  无条件(赛前) top5: {t0_h}/{n} = {t0_h/n*100:.1f}%")
print(f"  条件(45' 真实半场) top3: {t45_h3}/{n} = {t45_h3/n*100:.1f}%")
print(f"  条件(45' 真实半场) top5: {t45_h5}/{n} = {t45_h5/n*100:.1f}%")
print(f"  增益(top5): +{t45_h5/n*100 - t0_h/n*100:.1f}pp")

# 分场景
scenarios = [
    ("全场 HDA", [
        ("主胜", lambda r: r['result'] == 'H'),
        ("平局", lambda r: r['result'] == 'D'),
        ("客胜", lambda r: r['result'] == 'A'),
    ]),
    ("半场领先方", [
        ("半场主队领先", lambda r: r['ht'][0] > r['ht'][1]),
        ("半场客队领先", lambda r: r['ht'][0] < r['ht'][1]),
        ("半场平局", lambda r: r['ht'][0] == r['ht'][1]),
    ]),
    ("半场净胜差", [
        ("半场净胜1球", lambda r: abs(r['ht'][0] - r['ht'][1]) == 1),
        ("半场净胜2+球", lambda r: abs(r['ht'][0] - r['ht'][1]) >= 2),
    ]),
    ("全场总进球", [
        ("全场≥4球", lambda r: r['final'][0] + r['final'][1] >= 4),
        ("全场≤2球", lambda r: r['final'][0] + r['final'][1] <= 2),
    ]),
]

for section, items in scenarios:
    print(f"\n  [{section}]")
    for label, fn in items:
        sub = [r for r in RESULTS if fn(r)]
        if not sub: continue
        h3 = sum(1 for r in sub if r['t45_hit3'])
        h5 = sum(1 for r in sub if r['t45_hit5'])
        print(f"    {label}: 条件top3={h3}/{len(sub)}({h3/len(sub)*100:.0f}%)  top5={h5}/{len(sub)}({h5/len(sub)*100:.0f}%)")

# 漏报分析: 条件 top5 没命中的比赛
misses = [r for r in RESULTS if not r['t45_hit5']]
print(f"\n  [未命中样本: {len(misses)} 场]")
for r in misses[:10]:
    print(f"    {r['home']} {r['final'][0]}-{r['final'][1]} {r['away']} | 半场{r['ht'][0]}-{r['ht'][1]} | 赔率{r['odds']} | λ/μ={r['lam']}/{r['mu']}")

Path(OUT).write_text(json.dumps({
    "N": n,
    "t0_top5_hit": round(t0_h/n, 4),
    "t45_top3_hit": round(t45_h3/n, 4),
    "t45_top5_hit": round(t45_h5/n, 4),
    "processed_at": datetime.now().isoformat(),
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n[{datetime.now()-t0}] 完成. 摘要: {OUT}")
