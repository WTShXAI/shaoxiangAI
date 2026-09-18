"""Poisson GBM OU 前向纸盘 (Fix-2 落地, 2026-08-31)
================================================================
受控前向跟踪「poisson OU + 校准 + 市场融合」信号 (五道关结论: AUC 判别力真实,
+EV 未确立 → UNDERPOWERED_OR_UNSTABLE → 前向累积, ≥500 注复评)。

- 宇宙: events.db 干净完结场 (kickoff>=2026-07-01), IR-04 过滤
        (score_missing=1 排除 + 真实比分快照存在性), 主盘线走 build_opening_lines SSoT
- 模型: 验证版 Platt (poisson_ou_platt_fix2_20260831.joblib, 五道关拟合段产物,
        首次运行自动拟合落盘) → p_cal; 主口径 model_p = 0.7*p_cal + 0.3*implied
        (即五道关 C3 融合, w=0.7 为验证段搜出)
- 对照: 纯校准 (w=1.0) 与 生产 calibrator (ou_calibrator_20260830, fuse_w=0.30)
        的 n/ROI 仅记入 summary, 不进主表
- 下注: |model_p - implied| > 0.05 押差异侧, 平注 1 单位, 1/4 球正确清算
        (settle 逻辑直接复用 verify_poisson_ou_fivegates_20260831, 防两份口径漂移)
- 监控: 按 kickoff 切 Q 段逐段 ROI + bootstrap CI; decay_flag 诚实化(末段 CI 显著负 且 显著低于前期CI 才告警, 防小样本噪声误报)
- 输出: reports/paper_track_poisson_ou.csv (幂等重算) + summary json
- 铁律: 纯纸盘, 绝不产生真实下注 (IR-21 建仓须人工审批)

用法: runpy scripts/paper_track_poisson_ou_20260831.py [--since 2026-07-01]
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import sqlite3
import sys

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
CAL_PATH = os.path.join(ROOT, "models", "ou_calibrator_20260830.joblib")
PLATT_FIX2 = os.path.join(ROOT, "models", "poisson_ou_platt_fix2_20260831.joblib")

from pipeline.opening_line import build_opening_lines          # noqa: E402  IR-01
from pipeline.poisson_gbm import available as gbm_ok, predict_lambdas, p_over  # noqa: E402
from analysis.live_goal_probe import _open_1x2_from_snapshots   # noqa: E402
# 1/4 球清算复用五道关脚本 (同一份口径, 防漂移)
from scripts.verify_poisson_ou_fivegates_20260831 import settle  # noqa: E402

BET_GAP = 0.05   # 下注闸门 (与五道关一致)
FUSE_W = 0.7     # 主口径融合权重 = 五道关验证段搜出的 C3 best_w
FIT_FRAC = 0.6   # 验证版 Platt 拟合段比例 (与五道关一致)
Q = 5            # 时间分段数
REVIEW_N = 500   # 达到该下注数时提示五道关复评


def logit(p):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def platt_fix2(rec):
    """验证版 Platt: 与五道关完全同源的拟合 (rec 前 FIT_FRAC 段 fit, 落盘可复现)。"""
    if os.path.exists(PLATT_FIX2):
        d = joblib.load(PLATT_FIX2)
        print(f"[platt] 载入验证版 {PLATT_FIX2} (n_train={d.get('n_train')})")
        return d["logreg"]
    rec_s = sorted(rec, key=lambda r: r["ko"])
    k = int(len(rec_s) * FIT_FRAC)
    tr = rec_s[:k]
    raw_tr = np.array([r["p_raw"] for r in tr], float)
    y_tr = np.array([r["y"] for r in tr], int)
    lr = LogisticRegression(C=1e6).fit(logit(raw_tr).reshape(-1, 1), y_tr)
    joblib.dump({"logreg": lr, "n_train": len(tr),
                 "note": "验证版 Platt (Fix-2): 五道关拟合段产物, 与 verify_poisson_ou_fivegates "
                         "C3 同源; 纸盘主口径", "trained_at": int(__import__("time").time())},
                PLATT_FIX2)
    print(f"[platt] 拟合并落盘验证版 → {PLATT_FIX2} (n_train={len(tr)})")
    return lr


def implied_ou(ov, un):
    return (1.0 / ov) / ((1.0 / ov) + (1.0 / un))


def collect(since: str):
    op = build_opening_lines(DB, market="OU", full_time_only=True)
    op_map = {r["match_key"]: r for _, r in op.iterrows()}
    print(f"[collect] OU 主盘 SSoT: {len(op_map)} 场")

    # 生产 calibrator 仅作对照 (冻结标记: 验证性, 五道关显示参数敏感)
    try:
        cal_prod = joblib.load(CAL_PATH)
        lr_prod = cal_prod.get("logreg")
        w_prod = float(cal_prod.get("fuse_w", 0.30))
    except Exception:
        lr_prod, w_prod = None, 0.30

    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>=? "
        "ORDER BY kickoff ASC", (since,)).fetchall()

    rec, skip = [], {"fake0": 0, "no_snap": 0, "no_ou": 0, "no_1x2": 0, "gbm": 0}
    for mk, home, away, sh, sa, ko, league in rows:
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            skip["fake0"] += 1
            continue
        if not con.execute(
                "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            skip["no_snap"] += 1
            continue
        o = op_map.get(mk)
        if o is None:
            skip["no_ou"] += 1
            continue
        line, ov, un = float(o["line"]), float(o["over"]), float(o["under"])
        if not (ov > 1.01 and un > 1.01):
            skip["no_ou"] += 1
            continue
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            oh = od = oa = None
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            skip["no_1x2"] += 1
            continue
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
        if not lam:
            skip["gbm"] += 1
            continue
        tot = int(sh) + int(sa)
        y = 1 if tot > line else 0
        imp = implied_ou(ov, un)
        raw = float(p_over(lam[0], lam[1], line))
        # 生产 calibrator 对照概率
        p_cal_prod = raw
        if lr_prod is not None:
            raw_c = float(np.clip(raw, 1e-4, 1 - 1e-4))
            z = float(np.log(raw_c / (1 - raw_c)))
            p_cal_prod = float(lr_prod.predict_proba([[z]])[0][1])
        rec.append(dict(mk=mk, home=home, away=away, ko=ko, league=league or "?",
                        line=line, ov=ov, un=un, tot=tot, y=y, implied=imp,
                        p_raw=raw, p_cal_prod=p_cal_prod, w_prod=w_prod))
    con.close()
    print(f"[collect] 有效 {len(rec)} | 跳过 {skip}")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-01")
    args = ap.parse_args()

    if not gbm_ok():
        print("Poisson GBM 不可用, 中止")
        return

    rec = collect(args.since)
    rec.sort(key=lambda r: r["ko"])
    print(f"纸盘宇宙 n = {len(rec)}")

    # 验证版 Platt (五道关同源) → 主口径
    lr = platt_fix2(rec)
    for r in rec:
        raw_c = float(np.clip(r["p_raw"], 1e-4, 1 - 1e-4))
        z = float(np.log(raw_c / (1 - raw_c)))
        r["p_cal"] = float(lr.predict_proba([[z]])[0][1])
        r["model_p"] = FUSE_W * r["p_cal"] + (1 - FUSE_W) * r["implied"]
        r["model_p_prod"] = r["w_prod"] * r["p_cal_prod"] + (1 - r["w_prod"]) * r["implied"]

    rows = []
    cum = 0.0
    for r in rec:
        edge = r["model_p"] - r["implied"]
        side, odds, profit = "", 0.0, None
        if abs(edge) > BET_GAP:
            if edge > 0:
                side, odds = "over", r["ov"]
            else:
                side, odds = "under", r["un"]
            profit = settle(r["tot"], r["line"], r["ov"], r["un"], side)
            cum += profit
        rows.append(dict(
            match_key=r["mk"], ko=r["ko"], league=r["league"],
            home=r["home"], away=r["away"], line=r["line"],
            implied=round(r["implied"], 4), p_raw=round(r["p_raw"], 4),
            p_cal=round(r["p_cal"], 4), model_p=round(r["model_p"], 4),
            edge=round(edge, 4), side=side, odds=round(odds, 3) if odds else "",
            tot=r["tot"], y=r["y"],
            profit=round(profit, 3) if profit is not None else "",
        ))

    bets = [x for x in rows if x["side"]]
    n_bets = len(bets)
    roi = float(np.mean([x["profit"] for x in bets])) if bets else float("nan")
    cum_roi = cum / n_bets if n_bets else float("nan")

    # bootstrap CI
    roi_ci = [None, None]
    if n_bets >= 30:
        prof = np.array([float(x["profit"]) for x in bets])
        rng = np.random.default_rng(20260831)
        bs = np.array([prof[rng.integers(0, n_bets, n_bets)].mean() for _ in range(2000)])
        roi_ci = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]

    # 时间分段 ROI + 衰减监控 (诚实化: 段 ROI 须 bootstrap CI 显著负 且 显著低于前期 才告警)
    bets_sorted = sorted(bets, key=lambda x: x["ko"])
    segs = np.array_split(np.array(bets_sorted, dtype=object), Q) if n_bets >= Q else [np.array(bets_sorted, dtype=object)]
    seg_roi = []
    seg_ci = []          # (lo, hi) 或 None (段<30 注不估 CI)
    _rng = np.random.default_rng(20260901)
    for s in segs:
        if len(s) == 0:
            seg_roi.append(None); seg_ci.append(None); continue
        sp = np.array([float(x["profit"]) for x in s], float)
        seg_roi.append(float(sp.mean()))
        if len(s) >= 30:
            bs = np.array([sp[_rng.integers(0, len(s), len(s))].mean() for _ in range(2000)])
            seg_ci.append((float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))))
        else:
            seg_ci.append(None)
    decay_flag = False
    valid = [x for x in seg_roi if x is not None]
    if len(valid) >= 2:
        last_ci = seg_ci[-1]
        early_list = []
        for i in range(len(segs) - 1):
            if len(segs[i]):
                early_list.extend(float(x["profit"]) for x in segs[i])
        early = np.array(early_list, float)
        # 诚实衰减: 末段 CI 显著负(上界<0) 且 显著低于前期(CI 不重叠: 末段上界<前期下界)
        if last_ci is not None and len(early) >= 30:
            ebs = np.array([early[_rng.integers(0, len(early), len(early))].mean() for _ in range(2000)])
            early_lo = float(np.percentile(ebs, 2.5))
            early_hi = float(np.percentile(ebs, 97.5))
            if last_ci[1] < 0 and last_ci[1] < early_lo:
                decay_flag = True

    # 方向分解 (under 是五道关唯一方向性来源, 单独盯)
    n_over = len([x for x in bets if x["side"] == "over"])
    roi_over = float(np.mean([x["profit"] for x in bets if x["side"] == "over"])) if n_over else float("nan")
    n_under = len([x for x in bets if x["side"] == "under"])
    roi_under = float(np.mean([x["profit"] for x in bets if x["side"] == "under"])) if n_under else float("nan")

    # 对照口径: 纯校准 (w=1.0) 与 生产 calibrator (w_prod + 其 Platt)
    def ctrl_stats(key_p, w):
        n = r_ = 0.0
        for r in rec:
            mp = w * r[key_p] + (1 - w) * r["implied"]
            if abs(mp - r["implied"]) > BET_GAP:
                side = "over" if mp > r["implied"] else "under"
                n += 1
                r_ += settle(r["tot"], r["line"], r["ov"], r["un"], side)
        return {"n": int(n), "roi": round(r_ / n, 4) if n else None}

    ctrl_cal = ctrl_stats("p_cal", 1.0)
    ctrl_prod = ctrl_stats("p_cal_prod", rec[0]["w_prod"] if rec else 0.30)

    csv_path = os.path.join(ROOT, "reports", "paper_track_poisson_ou.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["match_key", "ko", "league", "home", "away", "line",
                                          "implied", "p_raw", "p_cal", "model_p", "edge",
                                          "side", "odds", "tot", "y", "profit"])
        w.writeheader()
        for x in rows:
            w.writerow(x)

    summary = dict(
        generated_at=datetime.datetime.now().astimezone().isoformat(),
        since=args.since, bet_gap=BET_GAP, fuse_w=FUSE_W,
        paper_universe_n=len(rec), bets_placed=n_bets,
        n_over=n_over, roi_over=round(roi_over, 4) if not np.isnan(roi_over) else None,
        n_under=n_under, roi_under=round(roi_under, 4) if not np.isnan(roi_under) else None,
        ctrl_cal_w1=ctrl_cal, ctrl_prod_calibrator=ctrl_prod,
        roi_per_bet=roi, cumulative_roi=cum_roi,
        roi_bootstrap_ci=[round(v, 4) if v is not None else None for v in roi_ci],
        segment_roi=[None if v is None else round(v, 4) for v in seg_roi],
        segment_ci=[[round(v[0], 4), round(v[1], 4)] if v is not None else None for v in seg_ci],
        decay_flag=decay_flag,
        review_due=(n_bets >= REVIEW_N),
        note=f"纯纸盘(无真实下注); 主口径=验证版Platt+w{FUSE_W}融合(五道关C3同源); "
             f"|model_p-implied|>{BET_GAP} 押差异侧; 1/4球正确清算; "
             f"五道关判定 UNDERPOWERED_OR_UNSTABLE → 前向累积; ≥{REVIEW_N} 注触发五道关复评",
    )
    with open(os.path.join(ROOT, "reports", "paper_track_poisson_ou_summary_20260831.json"),
              "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n[纸盘] 下注 {n_bets}/{len(rec)} 场 | 单注ROI={roi:+.2%} | 累计ROI={cum_roi:+.2%}")
    print(f"[方向] over n={n_over} ROI={roi_over:+.2%} | under n={n_under} ROI={roi_under:+.2%}")
    print(f"[CI]   ROI 95% [{roi_ci[0]:+.2%}, {roi_ci[1]:+.2%}]" if roi_ci[0] is not None else "[CI] 下注<30, 无CI")
    print(f"[时间分段 ROI] " + " | ".join(f"段{i+1}:{('—' if v is None else f'{v:+.2%}')}" for i, v in enumerate(seg_roi)))
    print(f"[衰减监控] {'🔴 衰减告警(decay_flag: 末段CI显著负且显著低于前期)' if decay_flag else '🟢 未检出显著衰减(末段CI跨零或与前期间方差内)'}")
    if summary["review_due"]:
        print(f"[复评] 已达 {n_bets} 注 → 重跑 verify_poisson_ou_fivegates_20260831.py 五道关复评")
    print(f"-> {csv_path} + paper_track_poisson_ou_summary_20260831.json")


if __name__ == "__main__":
    main()
