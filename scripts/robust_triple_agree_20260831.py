# -*- coding: utf-8 -*-
"""
robust_triple_agree_20260831.py — 三市场同向(TRIPLE_AGREE) 唯一正点估计候选的诚实压测

背景: dig_gq_crossmarket_coord 发现 IR-04 干净子集上
  1X2单跟随 -2.45% < +让球同向 -0.40% < +OU同向(TRIPLE) +10.06%  (单调梯度)
但 TRIPLE n=298, CI[-5.21,+26.23] 跨零 => 欠功率, 不可下结论。

本脚本五道关(IR-30):
  1. 扩样本: W=10/15/20 三窗口 + OU 双向同向变体(不只 over)
  2. 价格异质性: 剔除 price>5 长赔, 看是否少数高赔winner撑起 ROI
  3. 时间稳定性: 按 captured_at 三等分, 三段 ROI 是否同号
  4. +EV 硬门槛(用户口径): win_rate 是否 > 隐含概率 + 抽水
  5. 随机对照: 同等 n 随机选边 1000 次, TRIPLE ROI 是否落在随机分布尾部

输出: scripts/robust_triple_agree_out.json
"""
from __future__ import annotations
import sqlite3, json, re
import numpy as np

DB = "data/GQ.db"
OUT = "scripts/robust_triple_agree_out.json"
N_BOOT = 3000
MIN_MOVE = 0.03
SEL3 = {"home": 0, "draw": 1, "away": 2}
LINE_RE = re.compile(r"^(AH|OU)_(-?\d+(?:\.\d+)?)$")

con = sqlite3.connect(DB); con.execute("PRAGMA busy_timeout=30000"); cur = con.cursor()
cur.execute("SELECT home, away, score_home, score_away, result, kickoff FROM match_outcomes "
            "WHERE result IN ('home','draw','away') AND is_virtual=0 AND is_valid=1 "
            "AND score_home IS NOT NULL AND score_away IS NOT NULL")
score_map, res_map, ko_map = {}, {}, {}
for h, a, sh, sa, r, ko in cur.fetchall():
    k = f"{h} vs {a}"
    score_map[k] = (int(sh), int(sa)); res_map[k] = SEL3[r]; ko_map[k] = ko or ""
cur.execute("SELECT DISTINCT match_key FROM odds_changes "
            "WHERE score_at IS NOT NULL AND score_at!='' AND score_at!='0-0'")
has_ev = {r[0] for r in cur.fetchall()}
cur.execute("SELECT match_key, market, selection, change, to_odds, captured_at "
            "FROM odds_changes "
            "WHERE (market='1X2' "
            "   OR (market LIKE 'AH#_%' ESCAPE '#' AND market NOT LIKE 'AH#_1H#_%' ESCAPE '#' AND market NOT LIKE 'AH#_2H#_%' ESCAPE '#') "
            "   OR (market LIKE 'OU#_%' ESCAPE '#' AND market NOT LIKE 'OU#_1H#_%' ESCAPE '#' AND market NOT LIKE 'OU#_2H#_%' ESCAPE '#')) "
            "ORDER BY match_key, captured_at, id")
rows = cur.fetchall(); con.close()

G = {}
for mk, mkt, sel, chg, to_o, ts in rows:
    if mk in score_map and mk in has_ev:      # 直接只做 IR-04 干净子集
        G.setdefault(mk, []).append((mkt, sel, chg or 0.0, to_o, ts))
print(f"[universe] IR-04 干净 & 有终果 & 有tick: {len(G)} 场")


def side_of(ticks, opts):
    net, px = {}, {}
    for mkt, sel, chg, to_o, ts in ticks:
        net[sel] = net.get(sel, 0.0) + chg
        px[sel] = to_o
    c = {s: net.get(s, 0.0) for s in opts}
    if max(abs(v) for v in c.values()) < MIN_MOVE:
        return None, None, None
    s = min(c, key=c.get)
    return s, px.get(s), abs(c[s])


def collect(W, ou_mode="over"):
    """ou_mode: 'over'=仅OU-over被压低(原版) | 'either'=OU任一方被压低即算同向确认"""
    recs = []
    for mk, ch in G.items():
        x2 = [t for t in ch if t[0] == "1X2"]
        if len(x2) < W:
            continue
        early = x2[:W]; cutoff = early[-1][4]
        s2, p2, mag2 = side_of(early, ("home", "away"))
        if s2 is None or not p2 or p2 <= 1.01:
            continue
        # AH 主盘线
        ah = [t for t in ch if t[0].startswith("AH_") and t[4] <= cutoff]
        if not ah:
            continue
        cnt = {}
        for t in ah:
            cnt[t[0]] = cnt.get(t[0], 0) + 1
        m = LINE_RE.match(max(cnt, key=cnt.get))
        if not m:
            continue
        sA, _, _ = side_of([t for t in ah if t[0] == m.group(0)], ("home", "away"))
        if sA is None or sA != s2:
            continue                       # 必须 1X2 与 AH 同向
        # OU 主盘线
        ou = [t for t in ch if t[0].startswith("OU_") and t[4] <= cutoff]
        if not ou:
            continue
        cnt = {}
        for t in ou:
            cnt[t[0]] = cnt.get(t[0], 0) + 1
        mo = LINE_RE.match(max(cnt, key=cnt.get))
        if not mo:
            continue
        sO, _, _ = side_of([t for t in ou if t[0] == mo.group(0)], ("over", "under"))
        if sO is None:
            continue
        if ou_mode == "over" and sO != "over":
            continue
        pnl = (p2 - 1.0) if res_map[mk] == SEL3[s2] else -1.0
        recs.append({"mk": mk, "pnl": pnl, "px": p2, "win": res_map[mk] == SEL3[s2],
                     "ko": ko_map.get(mk, ""), "mag": mag2, "ou_side": sO})
    return recs


def summarize(recs, tag, cap=None):
    if cap is not None:
        recs = [r for r in recs if r["px"] <= cap]
    n = len(recs)
    if n == 0:
        return {"n": 0}
    pnl = np.array([r["pnl"] for r in recs]); px = np.array([r["px"] for r in recs])
    win = np.array([1.0 if r["win"] else 0.0 for r in recs])
    roi = float(pnl.mean()); wr = float(win.mean()); imp = float((1.0 / px).mean())
    rng = np.random.default_rng(23)
    b = rng.integers(0, n, size=(N_BOOT, n))
    rr = pnl[b].mean(axis=1)
    lo, hi = float(np.percentile(rr, 2.5)), float(np.percentile(rr, 97.5))
    # 随机对照: 同 n 同价格分布, 随机选边(用实际隐含概率作中奖概率)
    rp = rng.random((1000, n)) < (1.0 / px)
    rand_roi = np.where(rp, px - 1.0, -1.0).mean(axis=1)
    pct = float((rand_roi < roi).mean())
    return {"n": n, "roi": round(100 * roi, 2), "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
            "win_rate": round(100 * wr, 2), "implied": round(100 * imp, 2),
            "edge_pp": round(100 * (wr - imp), 2), "avg_price": round(float(px.mean()), 3),
            "rand_pctile": round(100 * pct, 1),
            "pos_ev": bool(roi > 0 and lo > 0 and n >= 300)}


out = {"meta": {"db": DB, "universe": len(G), "min_move": MIN_MOVE,
                "note": "全部为 IR-04 干净子集; 无前视(AH/OU 仅用 1X2 第W tick 之前)"}}

print("\n=== 关1: 扩样本(窗口 x OU模式) ===")
grid = {}
best = None
for W in (10, 15, 20):
    for mode in ("over", "either"):
        recs = collect(W, mode)
        s = summarize(recs, f"W{W}_{mode}")
        grid[f"W{W}_ou-{mode}"] = s
        print(f"  W={W:>2} ou={mode:6s} -> {s}")
        if s.get("n", 0) >= 300 and (best is None or s["roi"] > best[1]["roi"]):
            best = (f"W{W}_ou-{mode}", s, recs)
out["grid"] = grid

if best is None:
    out["verdict"] = "NO_CELL_REACHES_N300"
    print("\n[判定] 无任何格子达到 n>=300 => 全部欠功率, 不可下结论")
else:
    tag, s, recs = best
    print(f"\n[最佳格子] {tag} n={s['n']} ROI={s['roi']}%")
    print("\n=== 关2: 价格异质性(剔除长赔) ===")
    caps = {}
    for cap in (3.0, 4.0, 5.0, 99.0):
        cs = summarize(recs, tag, cap=cap)
        caps[f"px<={cap}"] = cs
        print(f"  px<={cap:<5} -> n={cs.get('n')} ROI={cs.get('roi')}% CI={cs.get('roi_CI')} edge={cs.get('edge_pp')}pp")
    out["price_caps"] = caps

    print("\n=== 关3: 时间稳定性(kickoff 三等分) ===")
    rs = sorted([r for r in recs if r["ko"]], key=lambda r: r["ko"])
    segs = {}
    if len(rs) >= 90:
        k = len(rs) // 3
        for i, part in enumerate([rs[:k], rs[k:2 * k], rs[2 * k:]], 1):
            ss = summarize(part, f"seg{i}")
            segs[f"seg{i}"] = ss
            print(f"  段{i} n={ss.get('n')} ROI={ss.get('roi')}% edge={ss.get('edge_pp')}pp")
    out["time_segments"] = segs
    signs = [v["roi"] for v in segs.values() if v.get("n")]
    out["time_all_positive"] = bool(signs and all(x > 0 for x in signs))

    print("\n=== 关4/5: +EV 硬门槛 + 随机对照 ===")
    print(f"  win_rate={s['win_rate']}% vs 隐含={s['implied']}% => edge={s['edge_pp']}pp")
    print(f"  随机同价选边分布中的百分位 = {s['rand_pctile']}%  (需 >97.5 才算显著优于随机)")
    out["best"] = {"tag": tag, **s}
    out["verdict"] = ("SURVIVES" if (s["pos_ev"] and out["time_all_positive"] and s["rand_pctile"] > 97.5)
                      else "UNDERPOWERED_OR_UNSTABLE")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n[done] {OUT}  判定={out.get('verdict')}")
