# -*- coding: utf-8 -*-
"""
黄金神瞳 · 手动扫描 vs 实际赛果 验证 (2026-09-01, v2 诚实版)
────────────────────────────────────────────────────────────────
对拍口径:
  - 只读 events.db 终场结果 + 开盘赔率(op_1x2_* = GQ 初盘, 与页面同源),
    重建黄金神瞳页面"可购买绿框"信号(天眼 side / 世界级 side), 与 final_result 对拍.
  - 纯验证, 绝不下注(IR-21), 绝不报"稳赢"(IR-30).
  - v2 修正(诚实):
      * 剔除 is_virtual=1 比赛 (IR-30: 虚拟盘不纳入可购买, 页面已落地过滤器).
      * 报告时间窗: kickoff>='2023-01-01' 占绝对多数 = 对 pre-2023 训练模型的真 OOS.
      * 加 bootstrap 95% CI, 判定 +EV 是否越过噪声(五道关 IR-04 同源).
      * 市场基线: 永远买开盘最低赔方(最受注方)的 ROI, 作对照.
判定:
  - 天眼绿框: oe_recommend ok 且 edge_pp>0, side=预测方.
  - 世界绿框: world_analyzer edge_1x2.edge_pp>=1.0, side=预测方 (代表性样本 2000).
  - hit: side==result(H/D/A). ROI(纸面, 单位注1): 命中+(odds-1), 未中-1.
"""
import os, sys, sqlite3, json, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.open_eye_predictor import recommend as oe_recommend, _covered

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db")
RMAP = {"home": "H", "draw": "D", "away": "A"}
SIDES = ("H", "D", "A")
OOS_CUT = "2023-01-01"


def world_side(league, oh, od, oa, home, away):
    try:
        from pipeline.world_analyzer import analyze_match
        wa = analyze_match(home, away, league, h=oh, d=od, a=oa)
        e = wa.get("edge_1x2") or {}
        ep = e.get("edge_pp"); s = e.get("side")
        if ep is not None and s in SIDES and ep >= 1.0:
            return s, ep
    except Exception:
        pass
    return None, None


def bootstrap_ci(pnls, n=2000, seed=20260901):
    """对单次注净收益序列做 bootstrap 95% CI (收益率% )."""
    if not pnls:
        return None, None, 0.0
    rnd = random.Random(seed)
    stakes = len(pnls)
    total = sum(pnls)
    roi = 100.0 * total / stakes
    idx = list(range(stakes))
    bs = []
    for _ in range(n):
        samp = [pnls[rnd.randrange(stakes)] for _ in idx]
        bs.append(100.0 * sum(samp) / stakes)
    bs.sort()
    lo = bs[int(0.025 * n)]; hi = bs[int(0.975 * n)]
    return round(lo, 2), round(hi, 2), round(roi, 2)


def main():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT mid,home,away,league,result,op_1x2_h,op_1x2_d,op_1x2_a,is_virtual,kickoff "
        "FROM match_outcomes WHERE result IS NOT NULL "
        "AND op_1x2_h IS NOT NULL AND op_1x2_d IS NOT NULL AND op_1x2_a IS NOT NULL"
    ).fetchall()
    c.close()

    # 仅纳入: 非虚拟 (IR-30) + 真 OOS (kickoff>=2023)
    pop = []
    dropped_virtual = dropped_insample = 0
    for r in rows:
        if r["is_virtual"] == 1:
            dropped_virtual += 1
            continue
        if (r["kickoff"] or "") < OOS_CUT:
            dropped_insample += 1
            continue
        pop.append(r)
    print("原始 finished+开盘价: %d | 剔虚拟: %d | 剔样本内(<2023): %d | 诚实 OOS 非虚拟: %d"
          % (len(rows), dropped_virtual, dropped_insample, len(pop)))

    t_green = t_hit = 0
    t_pnls = []; t_pps = []
    w_checked = w_green = w_hit = 0
    w_pnls = []; w_pps = []
    fav_hit = fav_stake = 0
    fav_pnls = []

    for r in pop:
        res = RMAP.get(r["result"])
        if res is None:
            continue
        oh, od, oa = float(r["op_1x2_h"]), float(r["op_1x2_d"]), float(r["op_1x2_a"])
        lg = r["league"]

        # 天眼
        ty_side = ty_ep = None
        if _covered(r["home"], r["away"]):
            try:
                rec = oe_recommend(r["home"], r["away"], oh, od, oa, None, lg)
                if rec.get("ok") and rec.get("edge_pp", 0) > 0:
                    ty_side = rec["side"]; ty_ep = rec["edge_pp"]
            except Exception:
                pass
        if ty_side:
            t_green += 1; t_pps.append(ty_ep)
            odds = (oh, od, oa)[SIDES.index(ty_side)]
            hit = (ty_side == res)
            t_pnls.append((odds - 1) if hit else -1)
            if hit:
                t_hit += 1

        # 世界 (代表性样本 2000)
        if w_checked < 2000:
            wd_side, wd_ep = world_side(lg, oh, od, oa, r["home"], r["away"])
            w_checked += 1
            if wd_side:
                w_green += 1; w_pps.append(wd_ep)
                odds = (oh, od, oa)[SIDES.index(wd_side)]
                hit = (wd_side == res)
                w_pnls.append((odds - 1) if hit else -1)
                if hit:
                    w_hit += 1

        # 市场基线: 买最低赔方
        fi = int(min(range(3), key=lambda i: (oh, od, oa)[i]))
        fs = SIDES[fi]; fo = (oh, od, oa)[fi]
        fav_stake += 1
        fh = (fs == res)
        fav_pnls.append((fo - 1) if fh else -1)
        if fh:
            fav_hit += 1

    t_lo, t_hi, t_roi = bootstrap_ci(t_pnls)
    w_lo, w_hi, w_roi = bootstrap_ci(w_pnls)
    f_lo, f_hi, f_roi = bootstrap_ci(fav_pnls)

    out = {
        "population": "非虚拟 + kickoff>=2023 (真OOS)",
        "n": len(pop),
        "tianyan": {
            "green": t_green, "hit": t_hit,
            "hit_rate": round(100.0 * t_hit / t_green, 2) if t_green else 0,
            "roi_pct": t_roi, "roi_ci95": [t_lo, t_hi],
            "edge_pp_mean": round(sum(t_pps) / len(t_pps), 2) if t_pps else None,
            "ev_verdict": "CI跨零=未确立+EV" if (t_lo < 0 < t_hi) else ("CI正=疑似+EV" if t_lo > 0 else "CI负"),
        },
        "world_sample2000": {
            "checked": w_checked, "green": w_green, "hit": w_hit,
            "hit_rate": round(100.0 * w_hit / w_green, 2) if w_green else 0,
            "roi_pct": w_roi, "roi_ci95": [w_lo, w_hi],
            "edge_pp_mean": round(sum(w_pps) / len(w_pps), 2) if w_pps else None,
            "ev_verdict": "CI跨零=未确立+EV" if (w_lo < 0 < w_hi) else ("CI正=疑似+EV" if w_lo > 0 else "CI负"),
        },
        "market_baseline_favorite": {
            "stake": fav_stake, "hit": fav_hit,
            "hit_rate": round(100.0 * fav_hit / fav_stake, 2),
            "roi_pct": f_roi, "roi_ci95": [f_lo, f_hi],
        },
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    rep = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_goldeneye_scan_vs_results.json")
    with open(rep, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n报告已写:", rep)


if __name__ == "__main__":
    main()
