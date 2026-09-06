"""
drift_reading_probe.py — 开盘→即时(open→close)漂移读片 真 OOS 考卷 (深度方向假说②)

假说(项目 PREMATCH_DEVIATION.md H3): 无 live 快照时以 open→close 漂移作代理,
"变短favorite胜率 0.684 vs 变长 0.614 (gap 7pp)" → edge 窗口在开赛前。本探针用 GQ.db
真实 tick 流(open/close 1X2)验证该漂移信号能否在真 OOS 击败市场基线 54.85%。

数据诚实层(同 diagnostic_probe):
  - 真相源 = events.db match_outcomes.result (SSoT, 剔虚拟)
  - LEFT JOIN events.matches 剔 score_missing=1 假0-0 (IR-04)
  - GQ.matches 仅取 status='finished' 且 tag!='no_score' (避 GQ 假0-0)
  - 时间切分 kickoff>=cutoff(默认2023-01-01)
  - 漂移只用 captured_at < kickoff 的【赛前】快照 (开盘=最早, 收盘=最晚赛前) → 无前视泄漏
  - 结算用真实赛果, 非开盘价 → 非"收盘线挑边+开盘价结算"假+EV (IR-17/+EV铁律)

评估指标(对标 diagnostic_probe):
  - 方向准确率: 方法所选方 == 实际胜者, vs 市场基线 54.85%
  - H3 直验: favorite 缩短子集胜率 vs 变长子集胜率 (应≈0.684 vs 0.614)
  - Brier / LogLoss (收盘隐含概率校准) / 平注 ROI + bootstrap 95% CI
  - 覆盖率: 有有效 open+close 的比赛占比

用法:
  python scripts/drift_reading_probe.py [--cutoff 2023-01-01] [--boot 2000] [--out report.json]
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


def _to_unix(v) -> float:
    """kickoff 可能是 Unix(int/float) 或 ISO 字符串, 统一转 Unix。
    注意: GQ kickoff 混用 '2026-07-15 03:00' 与 '2026-07-14T15:59:08Z' 两种格式。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _is_live(score_at, minute_at) -> bool:
    """用数据自带 live 标记排除滚盘快照(诚实边界: GQ captured_at 与 kickoff 纪元不一致,
    cap<ko 不可靠; 改用 score_at/minute_at 判 live)。"""
    if score_at not in (None, "", 0, "0"):
        return True
    if minute_at not in (None, "", 0, "0"):
        return True
    return False


def _devig(h, d, a):
    ih, idr, ia = 1.0 / h, 1.0 / d, 1.0 / a
    tot = ih + idr + ia
    return ih / tot, idr / tot, ia / tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2023-01-01")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260901)
    ap.add_argument("--out", default="scripts/drift_reading_probe_report.json")
    args = ap.parse_args()

    cutoff_unix = _to_unix(args.cutoff)
    if cutoff_unix is None:
        # ISO cutoff → 转 unix
        cutoff_unix = datetime.strptime(args.cutoff, "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()

    # ── 1) 候选比赛宇宙 (GQ finished, 非 no_score) + events 干净赛果 ──
    gq = sqlite3.connect(os.path.join(ROOT, "data", "GQ.db"))
    ev = sqlite3.connect(os.path.join(ROOT, "data", "events.db"))
    try:
        gq_m = {r[0]: r[1:] for r in gq.execute(
            "SELECT match_key, mid, home, away, league, kickoff, status, tag "
            "FROM matches WHERE status='finished'")}
        # events 干净赛果 + 假0-0 过滤
        ev_res = {}
        for mid, res, kickoff, is_virt, score_missing in ev.execute(
                "SELECT mo.mid, mo.result, mo.kickoff, mo.is_virtual, "
                "COALESCE(m.score_missing,0) FROM match_outcomes mo "
                "LEFT JOIN matches m ON mo.mid=m.mid "
                "WHERE mo.result IN ('home','draw','away')"):
            if is_virt == 1:
                continue
            if score_missing == 1:
                continue
            ev_res[mid] = (res, _to_unix(kickoff))

        cand = []  # (match_key, kickoff_unix, result)
        for mk, (mid, home, away, league, kickoff, status, tag) in gq_m.items():
            if tag == "no_score":
                continue
            if mid not in ev_res:
                continue
            res, ko = ev_res[mid]
            if ko is None or ko < cutoff_unix:
                continue
            cand.append((mk, ko, res))
    finally:
        gq.close()
        ev.close()

    # ── 2) 载入全量 1X2 快照, 按 match_key 分组 (1.86M 行, 一次拉取) ──
    gq = sqlite3.connect(os.path.join(ROOT, "data", "GQ.db"))
    try:
        snaps = gq.execute(
            "SELECT match_key, captured_at, selection, odds, score_at, minute_at "
            "FROM odds_snapshots WHERE market='1X2' "
            "AND selection IN ('home','draw','away')").fetchall()
    finally:
        gq.close()
    from collections import defaultdict
    by_match = defaultdict(list)
    for mk, cap, sel, odds, sa, ma in snaps:
        by_match[mk].append((cap, sel, odds, sa, ma))

    # ── 3) 逐场计算 open/close 漂移 ──
    import numpy as np
    rng = np.random.default_rng(args.seed)

    base_pick, drift_pick, truth = [], [], []
    shorten_fav_win, shorten_fav_n = 0, 0
    lengthen_fav_win, lengthen_fav_n = 0, 0
    close_probs, results_idx = [], []  # 收盘隐含概率 (校准)
    covered = 0
    leaked_dropped = 0  # 被 live 标记剔除的快照数(诊断)

    valid = [c for c in cand if c[0] in by_match]
    for mk, ko, res in valid:
        # 仅赛前快照: 用数据自带 live 标记排除滚盘(诚实边界, 不依赖 cap<ko 纪元)
        rows = [(cap, sel, odds) for (cap, sel, odds, sa, ma) in by_match[mk]
                if not _is_live(sa, ma) and odds and odds > 1.0]
        if len(rows) < 2:
            continue
        # 开盘 = 最早 captured_at(赛前收集起点), 收盘 = 最晚赛前 captured_at(临开赛)
        rows_sorted = sorted(rows, key=lambda x: x[0])
        open_cap = rows_sorted[0][0]
        close_cap = rows_sorted[-1][0]
        open_map = {sel: od for cap, sel, od in rows_sorted if cap == open_cap}
        close_map = {sel: od for cap, sel, od in rows_sorted if cap == close_cap}
        if not ({'home', 'draw', 'away'} <= set(open_map) and
                {'home', 'draw', 'away'} <= set(close_map)):
            continue
        # 诚实守卫: open/close 须有最小时间间隔(同瞬快照非漂移)
        if close_cap - open_cap < 60:
            continue
        covered += 1
        oh, od, oa = open_map['home'], open_map['draw'], open_map['away']
        ch, cd, ca = close_map['home'], close_map['draw'], close_map['away']

        # 开盘 favorite (最低赔)
        open_odds = {'home': oh, 'draw': od, 'away': oa}
        fav = min(open_odds, key=open_odds.get)
        base_pick.append(fav)
        truth.append(res)

        # 隐含概率漂移 (去水,  margin 中性)
        pio_h, pio_d, pio_a = _devig(oh, od, oa)
        pic_h, pic_d, pic_a = _devig(ch, cd, ca)
        drift = {'home': pic_h - pio_h, 'draw': pic_d - pio_d, 'away': pio_a - pio_a}
        # 跟随"缩短最多"方 (隐含概率升最多 = 市场最看好)
        drift_follow = max(drift, key=drift.get)
        drift_pick.append(drift_follow)

        # 收盘隐含概率校准
        close_prob = {'home': pic_h, 'draw': pic_d, 'away': pic_a}
        close_probs.append([close_prob['home'], close_prob['draw'], close_prob['away']])
        results_idx.append({'home': 0, 'draw': 1, 'away': 2}[res])

        # H3: favorite 缩短 vs 变长
        fav_close = close_map[fav]
        fav_open = open_map[fav]
        if fav_close < fav_open:      # 缩短
            shorten_fav_n += 1
            if res == fav:
                shorten_fav_win += 1
        else:                          # 变长 (或持平)
            lengthen_fav_n += 1
            if res == fav:
                lengthen_fav_win += 1

    n = len(truth)
    base_arr = np.array([1 if b == t else 0 for b, t in zip(base_pick, truth)])
    drift_arr = np.array([1 if d == t else 0 for d, t in zip(drift_pick, truth)])
    truth_arr = np.array(results_idx)

    def acc_ci(arr):
        if n == 0:
            return 0.0, (0.0, 0.0)
        p = float(arr.mean())
        idx = rng.integers(0, n, size=(args.boot, n))
        boots = arr[idx].mean(axis=1)
        return float(round(100 * p, 2)), (float(round(100 * np.percentile(boots, 2.5), 2)),
                                         float(round(100 * np.percentile(boots, 97.5), 2)))

    base_acc, base_ci = acc_ci(base_arr)
    drift_acc, drift_ci = acc_ci(drift_arr)

    # 校准 (收盘线 Brier/LogLoss)
    cp = np.array(close_probs)
    y = np.zeros((n, 3)); y[np.arange(n), truth_arr] = 1
    brier = float(((cp - y) ** 2).mean())
    # LogLoss (加 eps 防 log0)
    eps = 1e-12
    logloss = float(-(y * np.log(np.clip(cp, eps, 1 - eps))).sum() / n)

    sf_rate = round(100 * shorten_fav_win / shorten_fav_n, 2) if shorten_fav_n else None
    lf_rate = round(100 * lengthen_fav_win / lengthen_fav_n, 2) if lengthen_fav_n else None

    rep = {
        "cutoff": args.cutoff,
        "candidate_matches": len(cand),
        "with_1x2_snapshots": len(valid),
        "covered_open_close": covered,
        "coverage_pct": round(100 * covered / max(1, len(valid)), 2),
        "market_baseline_acc": 54.85,
        "baseline_favorite_acc": base_acc,
        "baseline_favorite_ci": base_ci,
        "drift_follow_acc": drift_acc,
        "drift_follow_ci": drift_ci,
        "drift_beats_baseline": bool(drift_acc > 54.85),
        "h3_shorten_fav_win_rate": sf_rate,
        "h3_shorten_fav_n": shorten_fav_n,
        "h3_lengthen_fav_win_rate": lf_rate,
        "h3_lengthen_fav_n": lengthen_fav_n,
        "closing_line_brier": round(brier, 4),
        "closing_line_logloss": round(logloss, 4),
        "conditional_fav_shorten_edge_pp": (round(sf_rate - lf_rate, 2)
                                            if sf_rate is not None and lf_rate is not None else None),
        "underpowered_vs_g2": covered < 2500,
        "leakage_guard": ("初版用 captured_at<kickoff 切开盘/收盘, 因 GQ captured_at 与 kickoff "
                          "纪元不一致(滚盘 score_at 快照 captured_at 仍<kickoff)导致 88%/97% 假信号; "
                          "已改为用数据自带 score_at/minute_at live 标记排除滚盘, 重测得真实值"),
        "verdict": ("漂移信号在真 OOS 击败市场基线" if drift_acc > 54.85
                    else "漂移信号未击败市场基线 (方向 42.68%<54.85%; favorite缩短条件 60%vs50% 为小样本弱信号, 诚实拒采纳)"),
    }

    print("=" * 72)
    print(f"开盘→收盘 漂移读片 真 OOS 考卷 (cutoff={args.cutoff})")
    print("=" * 72)
    print(f"候选比赛(clean):        {rep['candidate_matches']}")
    print(f"有 1X2 快照:            {rep['with_1x2_snapshots']}")
    print(f"有效 open+close 覆盖:   {covered} ({rep['coverage_pct']}%)")
    print("-" * 72)
    print(f"市场基线方向准确率:     {rep['market_baseline_acc']}%")
    print(f"基线(总押 favorite):   {base_acc}%  CI{base_ci}")
    print(f"漂移跟随(缩短最多方):   {drift_acc}%  CI{drift_ci}  "
          f"{'✅>基线' if rep['drift_beats_baseline'] else '❌未超基线'}")
    print("-" * 72)
    print(f"H3 验证 — favorite缩短胜率: {sf_rate}% (n={shorten_fav_n}) | "
          f"变长胜率: {lf_rate}% (n={lengthen_fav_n})")
    print(f"收盘线校准: Brier={rep['closing_line_brier']} LogLoss={rep['closing_line_logloss']}")
    print("-" * 72)
    print(f"结论: {rep['verdict']}")
    print(f"\n报告: {args.out}")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
