"""
independent_divergence_probe.py — Option B: 独立信息(开盘天眼) vs 盘口分歧 真 OOS +EV 验证

假说: 项目的 open_eye(independent_model) 作为"独立信息源", 其概率与市场去水隐含概率的
残差(edge_pp = model_prob - market_implied) 标识价值方; 押残差最大方(且 edge>0)应得 +EV。
这等价于用户说的"真杠杆=开盘时用独立信息预测谁缩水"。

为什么是 Option B 的可落地形态: 真·跨庄(多庄)数据当前没有(单庄 op_*), 故用项目自训的
independent_model_open_eye.joblib 作独立信息源 —— 它训练于 match_date<2023, 故 events.db
kickoff>=2023 是【真 OOS 时间外切分】。

诚实边界(对齐 +EV铁律 / IR-17 / IR-30):
  - 模型只输出建议, 不自动下注(IR-21)。
  - 结算用真实赛果; 下注价用【开盘价 op_1x2】(independent info as-of-kickoff, 非收盘线挑边)
    → 非"收盘线挑边+开盘价结算"假+EV。
  - +EV 唯一判据 = 实际胜率 > 隐含概率(1/赔率)+抽水; 报 ROI 必带 win_rate/implied/edge_pp。
  - 须与模型自带 OOF 指标(同 disjoint 2023+ 集)对照, 不一致即怀疑泄漏/乐观。

评估(对标 open_eye OOF_METRICS, 独立重算):
  - covered 覆盖率(两队均在 team_canonical, 否则天眼 PASS)
  - 方向准确率(谁赢) — 预期≈50%(已知无方向 edge, 非目标)
  - 平注 ROI + bootstrap 95% CI (押 recommended side @ 开盘赔)
  - 对照 BASE_MARKET(押 favorite @ 开盘赔) ROI
  - 按 edge_pp 分层(>0 / >3 / >5) 看 ROI 是否随阈值单调

用法:
  python scripts/independent_divergence_probe.py [--cutoff 2023-01-01] [--boot 2000] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
from pipeline import open_eye_predictor as oe


def _kickoff_to_date(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return ""
    s = str(v).strip()
    return s[:10] if s else ""


def _devig(h, d, a):
    ih, idr, ia = 1.0 / h, 1.0 / d, 1.0 / a
    tot = ih + idr + ia
    return ih / tot, idr / tot, ia / tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2023-01-01")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260902)
    ap.add_argument("--out", default="scripts/independent_divergence_probe_report.json")
    args = ap.parse_args()

    cutoff_unix = None
    try:
        cutoff_unix = datetime.strptime(args.cutoff, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        pass

    # ── 候选宇宙 (events.db match_outcomes, 诚实层) ──
    ev = sqlite3.connect(os.path.join(ROOT, "data", "events.db"))
    try:
        rows = ev.execute(
            "SELECT mo.mid, mo.home, mo.away, mo.league, mo.kickoff, mo.result, "
            "mo.op_1x2_h, mo.op_1x2_d, mo.op_1x2_a, "
            "COALESCE(m.score_missing,0) FROM match_outcomes mo "
            "LEFT JOIN matches m ON mo.mid=m.mid "
            "WHERE mo.is_virtual != 1 AND mo.result IN ('home','draw','away') "
            "AND mo.op_1x2_h IS NOT NULL AND mo.op_1x2_d IS NOT NULL "
            "AND mo.op_1x2_a IS NOT NULL AND COALESCE(m.score_missing,0) != 1"
        ).fetchall()
    finally:
        ev.close()

    rng = np.random.default_rng(args.seed)
    RES = {"H": "home", "D": "draw", "A": "away"}
    base_picks, drift_roi = [], []  # 占位
    rec_roi, rec_dir = [], []        # open_eye 推荐方 ROI / 方向
    base_roi, base_dir = [], []      # 押 favorite ROI / 方向
    edge_buckets = {"gt0": ([], []), "gt3": ([], []), "gt5": ([], [])}
    covered = 0
    uncovered = 0
    n_total = len(rows)

    for i, (mid, home, away, league, kickoff, result, oh, od, oa, sm) in enumerate(rows):
        ko = None
        if cutoff_unix is not None:
            ko = datetime.strptime(_kickoff_to_date(kickoff) or "1900-01-01",
                                    "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() \
                if _kickoff_to_date(kickoff) else None
            if ko is None or ko < cutoff_unix:
                continue
        if not (float(oh) > 1.0 and float(od) > 1.0 and float(oa) > 1.0):
            continue
        mdate = _kickoff_to_date(kickoff)
        # open_eye 推荐(独立信息 vs 盘口分歧)
        rec = oe.recommend(home, away, float(oh), float(od), float(oa), mdate, league)
        if not rec.get("ok"):
            uncovered += 1
            continue
        covered += 1
        side = rec["side"]            # H/D/A
        odds = float(rec["odds"])
        edge = float(rec["edge_pp"])
        win = RES[side] == result
        profit = (odds - 1.0) if win else -1.0
        rec_roi.append(profit)
        rec_dir.append(1 if win else 0)
        # edge 分层
        for tag, thr in (("gt0", 0), ("gt3", 3), ("gt5", 5)):
            if edge > thr:
                edge_buckets[tag][0].append(profit)
                edge_buckets[tag][1].append(1 if win else 0)
        # 对照: 押 favorite(最低开盘赔)
        imp_h, imp_d, imp_a = _devig(float(oh), float(od), float(oa))
        fav_idx = int(np.argmax([imp_h, imp_d, imp_a]))
        fav_side = ("H", "D", "A")[fav_idx]
        fav_odds = (float(oh), float(od), float(oa))[fav_idx]
        fav_win = RES[fav_side] == result
        base_roi.append((fav_odds - 1.0) if fav_win else -1.0)
        base_dir.append(1 if fav_win else 0)

        if (i + 1) % 500 == 0:
            print(f"  ...{i+1}/{n_total} processed (covered={covered})", flush=True)

    def stats(arr):
        a = np.array(arr, dtype=float)
        n = len(a)
        if n == 0:
            return dict(n=0, roi=None, ci=(None, None), win=None)
        roi = float(a.mean() * 100)
        idx = rng.integers(0, n, size=(args.boot, n))
        boots = a[idx].mean(axis=1) * 100
        return dict(n=n, roi=round(roi, 2),
                    ci=(round(float(np.percentile(boots, 2.5)), 2),
                        round(float(np.percentile(boots, 97.5)), 2)),
                    win=round(float(a.mean()) * 100, 2))

    s_rec = stats(rec_roi)
    s_base = stats(base_roi)
    s_rec_dir = stats(rec_dir)
    s_base_dir = stats(base_dir)
    buckets = {tag: {"n": len(p), "roi": round(float(np.mean(p) * 100), 2) if p else None,
                     "win": round(float(np.mean(d) * 100), 2) if d else None}
               for tag, (p, d) in edge_buckets.items()}

    rep = {
        "cutoff": args.cutoff,
        "n_total_universe": n_total,
        "covered_open_eye": covered,
        "uncovered_PASS": uncovered,
        "coverage_pct": round(100 * covered / max(1, covered + uncovered), 2),
        "open_eye_recommend": {**s_rec, "direction_acc": s_rec_dir["roi"], "pos_ev": (s_rec["ci"][0] or 0) > 0},
        "base_market_favorite": {**s_base, "direction_acc": s_base_dir["roi"], "pos_ev": (s_base["ci"][0] or 0) > 0},
        "edge_buckets": buckets,
        "model_oof_claim": {  # 模型自带 OOF(同 disjoint 2023+ 集) 对照
            "EYE_OPEN_ARGMAX_roi": 10.54, "EYE_OPEN_ARGMAX_CI": [5.01, 16.04],
            "EYE_OPEN_RESID_roi": 10.05, "EYE_OPEN_RESID_CI": [3.7, 16.36],
            "EYE_OPEN_RESID_004_roi": 15.59, "EYE_OPEN_RESID_004_CI": [9.32, 22.0],
            "BASE_MARKET_roi": -4.57,
        },
        "verdict": (
            "独立重验确认 open_eye +EV(ROI CI 不跨零为正) → 真信号, 可进五道关+人工审批(IR-21)"
            if (s_rec["ci"][0] or 0) > 0 else
            "独立重验未确认 +EV(ROI CI 跨零/负) → 模型 OOF 乐观或泄漏, 诚实拒采纳(IR-30)"),
    }

    print("=" * 74)
    print(f"Option B 独立信息(open_eye) vs 盘口分歧 真 OOS +EV 验证 (cutoff={args.cutoff})")
    print("=" * 74)
    print(f"宇宙: {n_total} | 天眼覆盖(covered): {covered} | PASS(uncovered): {uncovered} "
          f"({rep['coverage_pct']}%)")
    print("-" * 74)
    print(f"开盘天眼 推荐方:  ROI={s_rec['roi']}%  CI{s_rec['ci']}  方向{s_rec_dir['roi']}%  "
          f"pos_ev={rep['open_eye_recommend']['pos_ev']}  n={s_rec['n']}")
    print(f"基线 押favorite:  ROI={s_base['roi']}%  CI{s_base['ci']}  方向{s_base_dir['roi']}%  "
          f"pos_ev={rep['base_market_favorite']['pos_ev']}  n={s_base['n']}")
    print("-" * 74)
    print("edge_pp 分层 ROI:", {k: f"n={v['n']} roi={v['roi']}%" for k, v in buckets.items()})
    print("-" * 74)
    print(f"模型自带 OOF 对照: ARGMAX +10.54%[5.01,16.04] / RESID_004 +15.59%[9.32,22.0] / "
          f"BASE -4.57%")
    print(f"结论: {rep['verdict']}")
    print(f"\n报告: {args.out}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
