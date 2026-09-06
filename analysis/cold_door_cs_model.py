"""
冷门波胆 (Cold-Door Correct-Score) +EV 模型  —— 哨响AI
================================================
延续 cs_value_model.py（仅 0-0 单线）的思路，扩展到用户真正在押的**全冷门比分空间**
(0-0 / 1-0 / 2-1 / 3-1 / 1-4 ...)，并叠加 Favorite-Longshot Bias (FLB) 冷门惩罚。

方法论（严格、不伪造，遵循系统铁律#1/#3）：
  1. 公平概率 fair_P(score)：从 football_data.db.historical_matches (31.2万场, 含 open 1X2 + 真实比分)
     按"平局隐含概率 pd"（进球环境强代理）分桶，统计每桶内各比分的经验频率。
     → 这是真实赛果基准率，无庄家抽水。
  2. 盘口隐含概率 implied_P(score)：对单场 CS 网格的所有赔率做**去水归一**
     (每场跨比分 raw=1/odds 求和后归一)，隔离"真错价"而非 blanket 抽水。
  3. edge = fair_P - stripped_implied_P；对 odds>=3.0 的冷门施加 FLB 惩罚 edge*=0.5
     （7-25 实证：冷门被系统性高估，Q10 ROI −21.9%）。
  4. 回测 events.db 已完赛场（pre-match CS 开盘快照 + 真实赛果），对比三类策略 ROI/命中：
     A. 无脑买 0-0（cs_value_model 基线）
     B. +EV 过滤器（全比分，去水+FLB）
     C. 冷门-only（odds>=5 每场随机押 1 个，模拟用户"凭感觉买冷门"，多次抽样看方差）

诚实预期：唯一稳健 +EV 仍是 0-0；冷门比分去水后多数为 -EV，FLB 后更负 → 用户"中冷门"
     主要是方差红利 + FLB 高估，而非可复制的错价 edge。
"""
import sqlite3, json, re, random
import numpy as np

FOOT = "D:/Architecture/data/football_data.db"
GQ = "D:/Architecture/data/events.db"
RNG = random.Random(20260814)

SCORE_RE = re.compile(r"^(\d+)-(\d+)$")
MAX_GOAL = 4  # 标准网格 0-0..4-4


def _margin_strip(h, d, a):
    s = 1.0 / h + 1.0 / d + 1.0 / a
    return 1.0 / h / s, 1.0 / d / s, 1.0 / a / s


# ---------------------------------------------------------------- 1) 校准
def calibrate(n_bins=10):
    """返回 (bins_edges, list_of_{score:freq}), 以及每桶样本数。"""
    f = sqlite3.connect(FOOT)
    rows = f.execute("""
        SELECT open_home_odds, open_draw_odds, open_away_odds, home_score, away_score
        FROM historical_matches
        WHERE open_home_odds>1.01 AND open_draw_odds>1.01 AND open_away_odds>1.01
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """).fetchall()
    f.close()
    pds, scores = [], []
    for h, d, a, hs, as_ in rows:
        _, dp, _ = _margin_strip(h, d, a)
        pds.append(dp)
        scores.append((int(hs), int(as_)))
    pds = np.array(pds)
    edges = np.quantile(pds, np.linspace(0, 1, n_bins + 1))
    edges[0] -= 1e-9; edges[-1] += 1e-9
    bins = np.digitize(pds, edges)
    out = []
    for b in range(1, n_bins + 1):
        idx = bins == b
        n = int(idx.sum())
        if n < 500:
            out.append(None); continue
        cnt = {}
        for hs, as_ in [scores[i] for i in np.where(idx)[0]]:
            key = f"{hs}-{as_}" if hs <= MAX_GOAL and as_ <= MAX_GOAL else "其他"
            cnt[key] = cnt.get(key, 0) + 1
        tot = sum(cnt.values())
        out.append({k: v / tot for k, v in cnt.items()})
    return edges, out, len(rows)


def fair_prob(home_odds, draw_odds, away_odds, edges, calib):
    _, dp, _ = _margin_strip(home_odds, draw_odds, away_odds)
    b = int(np.digitize([dp], edges)[0])
    if b < 1 or b > len(calib) or calib[b - 1] is None:
        # 回退到最近非空桶
        for bb in range(len(calib)):
            if calib[bb] is not None:
                return calib[bb], dp
        return {}, dp
    return calib[b - 1], dp


# ---------------------------------------------------------------- 2) 回测
def backtest(edges, calib, thresh=0.03, flb_penalty=0.5, n_rand=500):
    g = sqlite3.connect(GQ)
    g.row_factory = sqlite3.Row
    # 完赛且有 CS 赔率的比赛
    mrows = g.execute("""
        SELECT home, away, score_home, score_away FROM matches
        WHERE score_home IS NOT NULL AND score_away IS NOT NULL AND status='finished'
    """).fetchall()

    def earliest_cs(mk):
        """返回 {scoreline: (odds, cap_at)} 取每比分最早(赛前)快照。"""
        rows = g.execute("""
            SELECT selection, odds, captured_at FROM odds_snapshots
            WHERE match_key=? AND market='CS' AND selection GLOB '[0-9]-[0-9]'
            ORDER BY captured_at ASC
        """, (mk,)).fetchall()
        d = {}
        for r in rows:
            s = r["selection"]
            if s in d:
                continue
            d[s] = float(r["odds"])
        return d

    def early_1x2(mk):
        d = {}
        for sel in ("home", "draw", "away"):
            r = g.execute("""
                SELECT odds FROM odds_snapshots WHERE match_key=? AND market='1X2' AND selection=?
                ORDER BY captured_at ASC LIMIT 1
            """, (mk, sel)).fetchone()
            if r:
                d[sel] = float(r["odds"])
        return d

    A_f = A_w = 0; A_pnl = 0.0           # 策略A: 无脑买0-0
    B_f = B_w = 0; B_pnl = 0.0           # 策略B: +EV过滤(全比分)
    C_f = C_w = 0; C_pnl = 0.0           # 策略C: 冷门随机押(模拟用户)
    tested = 0
    per_score_ev = {}                    # 各比分被flag后的累计 edge/命中
    for home, away, sh, sa in mrows:
        mk = f"{home} vs {away}"
        cs = earliest_cs(mk)
        if "0-0" not in cs:
            continue
        o1x2 = early_1x2(mk)
        if not ("home" in o1x2 and "draw" in o1x2 and "away" in o1x2):
            continue
        tested += 1
        is00 = 1 if (sh == 0 and sa == 0) else 0
        # 策略A
        c00 = cs["0-0"]
        A_f += 1
        if is00: A_w += 1; A_pnl += c00 - 1
        else: A_pnl -= 1
        # 去水归一 CS 隐含
        raw = {s: 1.0 / o for s, o in cs.items() if o > 1.01}
        tot = sum(raw.values())
        strip = {s: v / tot for s, v in raw.items()}
        fair, dp = fair_prob(o1x2["home"], o1x2["draw"], o1x2["away"], edges, calib)
        if not fair:
            continue
        # 策略B: 对每个比分算 edge
        cold_bets = []
        for s, o in cs.items():
            m = SCORE_RE.match(s)
            if not m:
                continue
            fp = fair.get(s, fair.get("其他", 0.0))
            edge = fp - strip.get(s, 0.0)
            if o >= 3.0:
                edge *= flb_penalty
            per_score_ev.setdefault(s, [0, 0, 0.0])  # flags, wins, edge_sum
            per_score_ev[s][2] += edge
            if edge >= thresh:
                per_score_ev[s][0] += 1
                hit = 1 if (sh == int(m.group(1)) and sa == int(m.group(2))) else 0
                if hit:
                    per_score_ev[s][1] += 1
                cold_bets.append((s, o, hit))
        for s, o, hit in cold_bets:
            B_f += 1
            if hit: B_w += 1; B_pnl += o - 1
            else: B_pnl -= 1
        # 策略C: 冷门随机押 (odds>=5)，模拟"凭感觉买冷门"
        cold = [(s, o) for s, o in cs.items() if o >= 5.0 and SCORE_RE.match(s)]
        if cold:
            for _ in range(n_rand):
                s, o = RNG.choice(cold)
                m = SCORE_RE.match(s)
                hit = 1 if (sh == int(m.group(1)) and sa == int(m.group(2))) else 0
                C_f += 1
                if hit: C_w += 1; C_pnl += o - 1
                else: C_pnl -= 1
    g.close()
    return {
        "tested": tested,
        "A_00_always": _stat(A_f, A_w, A_pnl),
        "B_ev_filter": _stat(B_f, B_w, B_pnl),
        "C_cold_random": _stat(C_f, C_w, C_pnl),
        "per_score_ev": per_score_ev,
    }


def _stat(f, w, pnl):
    return {"flags": f, "wins": w,
            "hit": (w / f) if f else 0,
            "roi": (pnl / f) if f else 0}


if __name__ == "__main__":
    print("[1/3] 校准公平比分分布 (31万场, 按 pd 分桶) ...")
    edges, calib, n = calibrate()
    nbin = sum(1 for c in calib if c is not None)
    print(f"  样本={n}, 有效桶={nbin}, pd区间=[{edges[0]:.3f},{edges[-1]:.3f}]")
    # 打印 0-0 的跨桶经验率（应与 cs_value_model 方向一致：低 pd→低 0-0率）
    print("  0-0 经验率(低pd→高pd桶):",
          [round(c.get("0-0", 0), 4) for c in calib if c])

    print("[2/3] 回测 GQ 完赛场 (pre-match CS 去水 + FLB) ...")
    r = backtest(edges, calib)
    print(json.dumps({k: v for k, v in r.items() if k != "per_score_ev"},
                     indent=2, ensure_ascii=False))

    print("[3/3] 各比分 +EV 画像 (按 flag 数降序, 仅列 top15 + 0-0) ...")
    ps = r["per_score_ev"]
    items = sorted(ps.items(), key=lambda kv: kv[1][0], reverse=True)
    print(f"  {'比分':<5}{'flag数':>8}{'命中':>6}{'命中率':>8}{'平均edge':>10}")
    shown = 0
    for s, (fl, wi, es) in items:
        if shown >= 15 and s != "0-0":
            continue
        avg = es / fl if fl else 0
        print(f"  {s:<5}{fl:>8}{wi:>6}{wi/fl if fl else 0:>8.3f}{avg:>10.4f}")
        shown += 1

    # 固化结果
    out = {
        "tested": r["tested"],
        "A_00_always": r["A_00_always"],
        "B_ev_filter": r["B_ev_filter"],
        "C_cold_random": r["C_cold_random"],
        "per_score_ev": {s: {"flags": v[0], "wins": v[1],
                             "hit": v[1] / v[0] if v[0] else 0,
                             "avg_edge": v[2] / v[0] if v[0] else 0}
                         for s, v in ps.items() if v[0] > 0},
        "calib_00_by_pd": [round(c.get("0-0", 0), 4) for c in calib if c],
        "thresh": 0.03, "flb_penalty": 0.5,
    }
    json.dump(out, open("analysis/cold_door_cs_model_result.json", "w"),
              indent=2, ensure_ascii=False)
    print("\n[saved] analysis/cold_door_cs_model_result.json")
