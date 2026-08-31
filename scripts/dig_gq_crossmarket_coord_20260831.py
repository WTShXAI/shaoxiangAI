# -*- coding: utf-8 -*-
"""
dig_gq_crossmarket_coord_20260831.py
用户 2026-08-31: "盘口的变化除了比分, 往往其它的 1x2 让球、ou 都是 0.01-1.5 之间的值再轮流变化。深挖"

上一轮只测了 1X2 单市场早期移动(全负)。本轮补齐真正的核心假设:
    **跨市场协同** —— 1X2 与 让球(AH) 同向压低时, 是否过滤掉噪声、露出真实 +EV?
    (若 tick 只是做市噪声, 三个市场应各自随机; 若是聪明钱, 应协同)

严格无前视设计:
  1. 取每场 1X2 前 W 个 tick, 记切点时间 cutoff = 第 W 个 tick 的 captured_at
  2. AH / OU 只用 captured_at <= cutoff 的 tick (绝不看未来)
  3. 下注价 = 该方在窗口结束时的**当前赔率**(可观测)
  4. 结算 = match_outcomes 真实比分 (1/4 球线按半注拆分正确清算)

主盘线选取: 该场 tick 数最多的全场 AH_x / OU_x (排除 1H/2H)

诚实守卫(IR-30): n>=300 且 bootstrap 95% CI 下限 > 0 才认 +EV。
              同时跑 IR-04 假 0-0 过滤前后两版, 防污染造假。
输出: scripts/dig_gq_crossmarket_coord_out.json
"""
from __future__ import annotations
import sqlite3, json, re
import numpy as np

DB = "data/GQ.db"
OUT = "scripts/dig_gq_crossmarket_coord_out.json"
N_BOOT = 2000
W = 10                      # 早期窗口 tick 数
MIN_MOVE = 0.03             # 实质移动门槛
SEL3 = {"home": 0, "draw": 1, "away": 2}

# ---------------------------------------------------------------- 清算函数
def _split(line: float):
    """1/4 球线拆两半注; 整/半线返回单线"""
    if abs(round(line * 4) - line * 4) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9:
        return [line - 0.25, line + 0.25]   # 例: -0.25 -> [-0.5, 0.0]
    return [line]

def settle_ah(side: str, line: float, o: float, sh: int, sa: int) -> float:
    """让球清算; line = 主队让球数(AH_-0.25 -> -0.25). 返回单位注收益"""
    tot = 0.0
    parts = _split(line)
    for L in parts:
        m = (sh - sa) + L if side == "home" else (sa - sh) - L
        tot += (o - 1.0) if m > 0 else (0.0 if abs(m) < 1e-9 else -1.0)
    return tot / len(parts)

def settle_ou(side: str, line: float, o: float, sh: int, sa: int) -> float:
    tot = 0.0
    parts = _split(line)
    for L in parts:
        m = (sh + sa) - L
        m = m if side == "over" else -m
        tot += (o - 1.0) if m > 0 else (0.0 if abs(m) < 1e-9 else -1.0)
    return tot / len(parts)

def stats(pnl, prices, label):
    pnl = np.asarray(pnl, dtype=float)
    n = len(pnl)
    if n == 0:
        return {"n": 0}
    roi = float(pnl.mean())
    rng = np.random.default_rng(11)
    b = rng.integers(0, n, size=(N_BOOT, n))
    rois = pnl[b].mean(axis=1)
    lo, hi = float(np.percentile(rois, 2.5)), float(np.percentile(rois, 97.5))
    d = {"n": n, "roi": round(100 * roi, 2),
         "roi_CI": [round(100 * lo, 2), round(100 * hi, 2)],
         "pos_ev": bool(roi > 0 and lo > 0 and n >= 300)}
    if prices is not None and len(prices):
        p = np.asarray(prices, dtype=float)
        d["avg_price"] = round(float(p.mean()), 3)
        d["implied_single"] = round(100 * float((1.0 / p).mean()), 2)
        d["win_rate"] = round(100 * float((pnl > 0).mean()), 2)
    return d

# ---------------------------------------------------------------- 载入
con = sqlite3.connect(DB); con.execute("PRAGMA busy_timeout=30000"); cur = con.cursor()

cur.execute("SELECT home, away, score_home, score_away, result FROM match_outcomes "
            "WHERE result IN ('home','draw','away') AND is_virtual=0 AND is_valid=1 "
            "AND score_home IS NOT NULL AND score_away IS NOT NULL")
score_map, res_map = {}, {}
for h, a, sh, sa, r in cur.fetchall():
    k = f"{h} vs {a}"
    score_map[k] = (int(sh), int(sa)); res_map[k] = SEL3[r]
print(f"[results] match_outcomes 可用终果+比分: {len(score_map)} 场")

# IR-04 假 0-0 证据集: 有非零 score_at 快照 => 比分真实被采到过
cur.execute("SELECT DISTINCT match_key FROM odds_changes "
            "WHERE score_at IS NOT NULL AND score_at!='' AND score_at!='0-0'")
has_evidence = {r[0] for r in cur.fetchall()}
print(f"[IR-04] 有非零 score_at 证据的比赛: {len(has_evidence)}")

# tick 流: 1X2 + 全场 AH_* + 全场 OU_* (排除 1H/2H)
cur.execute("SELECT match_key, market, selection, change, to_odds, from_odds, captured_at, minute_at "
            "FROM odds_changes "
            "WHERE (market='1X2' "
            "   OR (market LIKE 'AH#_%' ESCAPE '#' AND market NOT LIKE 'AH#_1H#_%' ESCAPE '#' AND market NOT LIKE 'AH#_2H#_%' ESCAPE '#') "
            "   OR (market LIKE 'OU#_%' ESCAPE '#' AND market NOT LIKE 'OU#_1H#_%' ESCAPE '#' AND market NOT LIKE 'OU#_2H#_%' ESCAPE '#')) "
            "ORDER BY match_key, captured_at, id")
rows = cur.fetchall()
con.close()
print(f"[ticks] 载入 1X2+AH+OU 变动行: {len(rows):,}")

G = {}
for mk, mkt, sel, chg, to_o, from_o, ts, mt in rows:
    if mk not in score_map:
        continue
    G.setdefault(mk, []).append((mkt, sel, chg, to_o, from_o, ts, mt))
print(f"[groups] 与终果可对齐的比赛: {len(G)}")

LINE_RE = re.compile(r"^(AH|OU)_(-?\d+(?:\.\d+)?)$")

def net_and_price(ticks):
    """返回 (net_change_by_selection, 窗口结束时当前价, 开盘价)"""
    net, cur_p, op_p = {}, {}, {}
    for mkt, sel, chg, to_o, from_o, ts, mt in ticks:
        net[sel] = net.get(sel, 0.0) + (chg or 0.0)
        cur_p[sel] = to_o
        op_p.setdefault(sel, from_o)
    return net, cur_p, op_p

# ---------------------------------------------------------------- 主循环
buckets = {k: {"pnl": [], "px": []} for k in [
    "X2_ALL", "X2_AGREE", "X2_DISAGREE", "X2_AGREE_STRONG",
    "AH_AGREE", "AH_ALL", "OU_FOLLOW", "OU_FADE",
    "TRIPLE_AGREE", "X2_AGREE_LOOKAHEAD"]}
buckets_clean = {k: {"pnl": [], "px": []} for k in buckets}

diag = {"no_1x2": 0, "short_1x2": 0, "no_move": 0, "no_ah": 0, "no_ou": 0,
        "agree": 0, "disagree": 0, "used_x2": 0}

for mk, ch in G.items():
    sh, sa = score_map[mk]
    clean = (mk in has_evidence)          # IR-04: 有非零比分证据
    x2 = [t for t in ch if t[0] == "1X2"]
    if len(x2) < W:
        diag["short_1x2"] += 1; continue
    early_x2 = x2[:W]
    cutoff = early_x2[-1][5]
    net2, px2, op2 = net_and_price(early_x2)
    # 只比主客(让球无平局, 才能跨市场对齐)
    cand = {s: net2.get(s, 0.0) for s in ("home", "away")}
    if max(abs(v) for v in cand.values()) < MIN_MOVE:
        diag["no_move"] += 1; continue
    side2 = min(cand, key=cand.get)          # 被压低最多 = 早期资金方向
    p2 = px2.get(side2)
    if not p2 or p2 <= 1.01:
        continue
    pnl2 = (p2 - 1.0) if res_map[mk] == SEL3[side2] else -1.0
    diag["used_x2"] += 1

    def push(key, v, px, cl=clean):
        buckets[key]["pnl"].append(v); buckets[key]["px"].append(px)
        if cl:
            buckets_clean[key]["pnl"].append(v); buckets_clean[key]["px"].append(px)

    push("X2_ALL", pnl2, p2)

    # ---- 前视上界对照: 全窗口方向 + 开盘价
    netF, pxF, opF = net_and_price(x2)
    candF = {s: netF.get(s, 0.0) for s in ("home", "away")}
    if max(abs(v) for v in candF.values()) >= MIN_MOVE:
        sF = min(candF, key=candF.get); pF = opF.get(sF)
        if pF and pF > 1.01:
            push("X2_AGREE_LOOKAHEAD", (pF - 1.0) if res_map[mk] == SEL3[sF] else -1.0, pF)

    # ---- 让球: 主盘线 = 切点前 tick 最多的全场 AH_x
    ah_pre = [t for t in ch if t[0].startswith("AH_") and t[5] <= cutoff]
    ah_line = ah_side = None
    if ah_pre:
        cnt = {}
        for t in ah_pre:
            cnt[t[0]] = cnt.get(t[0], 0) + 1
        main_ah = max(cnt, key=cnt.get)
        m = LINE_RE.match(main_ah)
        if m:
            ah_line = float(m.group(2))
            sub = [t for t in ah_pre if t[0] == main_ah]
            netA, pxA, _ = net_and_price(sub)
            cA = {s: netA.get(s, 0.0) for s in ("home", "away")}
            if max(abs(v) for v in cA.values()) >= MIN_MOVE:
                ah_side = min(cA, key=cA.get)
                pA = pxA.get(ah_side)
                if pA and pA > 1.01:
                    push("AH_ALL", settle_ah(ah_side, ah_line, pA, sh, sa), pA)
    else:
        diag["no_ah"] += 1

    # ---- 协同判定
    if ah_side is not None:
        if ah_side == side2:
            diag["agree"] += 1
            push("X2_AGREE", pnl2, p2)
            # 强协同: 两市场净移动都 >= 0.05
            if abs(cand[side2]) >= 0.05:
                push("X2_AGREE_STRONG", pnl2, p2)
            sub = [t for t in ah_pre if t[0] == f"AH_{ah_line:g}" or LINE_RE.match(t[0]) and float(LINE_RE.match(t[0]).group(2)) == ah_line]
            _, pxA2, _ = net_and_price(sub)
            pA2 = pxA2.get(ah_side)
            if pA2 and pA2 > 1.01:
                push("AH_AGREE", settle_ah(ah_side, ah_line, pA2, sh, sa), pA2)
        else:
            diag["disagree"] += 1
            push("X2_DISAGREE", pnl2, p2)

    # ---- 大小球: 主盘线 = 切点前 tick 最多的全场 OU_x
    ou_pre = [t for t in ch if t[0].startswith("OU_") and t[5] <= cutoff]
    ou_side = ou_line = None
    if ou_pre:
        cnt = {}
        for t in ou_pre:
            cnt[t[0]] = cnt.get(t[0], 0) + 1
        main_ou = max(cnt, key=cnt.get)
        m = LINE_RE.match(main_ou)
        if m:
            ou_line = float(m.group(2))
            sub = [t for t in ou_pre if t[0] == main_ou]
            netO, pxO, _ = net_and_price(sub)
            cO = {s: netO.get(s, 0.0) for s in ("over", "under")}
            if max(abs(v) for v in cO.values()) >= MIN_MOVE:
                ou_side = min(cO, key=cO.get)
                pO = pxO.get(ou_side)
                if pO and pO > 1.01:
                    push("OU_FOLLOW", settle_ou(ou_side, ou_line, pO, sh, sa), pO)
                rev = "under" if ou_side == "over" else "over"
                pR = pxO.get(rev)
                if pR and pR > 1.01:
                    push("OU_FADE", settle_ou(rev, ou_line, pR, sh, sa), pR)
    else:
        diag["no_ou"] += 1

    # ---- 三市场齐同向(1X2+AH 同向 且 OU over 被压低 = 一致看涨主队进球)
    if ah_side is not None and ah_side == side2 and ou_side == "over":
        push("TRIPLE_AGREE", pnl2, p2)

# ---------------------------------------------------------------- 汇总
print("\n[diag]", diag)
res_all = {k: stats(v["pnl"], v["px"], k) for k, v in buckets.items()}
res_cln = {k: stats(v["pnl"], v["px"], k) for k, v in buckets_clean.items()}

print("\n=== 全量(未过滤假0-0) ===")
for k, v in res_all.items():
    print(f"  {k:22s} {v}")
print("\n=== IR-04 干净子集(有非零 score_at 证据) ===")
for k, v in res_cln.items():
    print(f"  {k:22s} {v}")

pos = [k for k, v in res_cln.items() if v.get("pos_ev")]
out = {"meta": {"db": DB, "W": W, "min_move": MIN_MOVE,
                "n_matches_aligned": len(G), "diag": diag,
                "note": "无前视: AH/OU 仅用 1X2 第W个tick时间之前的变动; 1/4球线半注拆分清算"},
       "all": res_all, "clean_ir04": res_cln, "pos_ev_clean": pos}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"\n[done] 写出 {OUT}")
print("[诚实判定] 协同(AGREE) 若显著优于 ALL 且 CI 下限>0 => 跨市场协同是真信号(用户假设成立);")
print("          若 AGREE 仍负/CI跨零 => tick 轮动是做市噪声, 协同不产生可交易 edge.")
