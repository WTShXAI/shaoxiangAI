"""
opening_price_analysis.py — 开盘价预判分析 (OLPO: Opening-Line Predicted Outcome)
==================================================================================

涛哥洞察 (2026-08-11):
  当一方领先后赔率大幅坍塌 (主 35→1.16, 客 60→1.01), 此时记录的收盘赔率
  已反映赛果事实, 对预测无价值。反向思考: 开盘价 (赛前 T-30min ~ T-0)
  是市场对该比赛赛前的真实预期, 结合赛果可作为预测源。

三层分析:
  Layer 1 — 开盘价校准度: 开盘隐含概率 vs 实际胜率 (Brier / ECE / 分箱)
  Layer 2 — 开盘价方向一致性: 开盘价强烈暗示的方向, 实际命中率是多少
  Layer 3 — 反事实价值场次: 开盘价强烈暗示某方向、但实际颠覆的场次
            提取这些场次的特征模式, 寻找结构性 edge

数据来源:
  - events.db.odds_snapshots (29M, 全 minute_at=0 = 赛前快照)
  - events.db.match_outcomes (5502 完赛 + 开盘赔率)

用法:
    from pipeline.opening_price_analysis import opening_calibration, upset_patterns
    cal = opening_calibration()
    upsets = upset_patterns(min_fav_prob=0.65)
"""
from __future__ import annotations

import os
import sqlite3
import logging
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GQ_DB = os.path.join(ROOT, "data", "events.db")


# ────────────────────────────────────────────────────────────────────────────
# 数据加载
# ────────────────────────────────────────────────────────────────────────────

def _load_opening_with_outcome(db_path: str | None = None) -> pd.DataFrame:
    """加载每场比赛的开盘价 + 赛果, 联表匹配。

    Returns DataFrame 列:
      match_key, opened_at, home, draw, away, overround,
      p_home_devig, p_draw_devig, p_away_devig,
      score_home, score_away, result, league, kickoff
    """
    from pipeline.opening_line import build_opening_lines
    op = build_opening_lines(market="1X2", db_path=db_path)

    conn = sqlite3.connect(db_path or GQ_DB)
    try:
        # match_outcomes 用 mid 数字 ID; 联表需通过 home+away 中文名构造 match_key
        out_sql = ("SELECT mid, home, away, score_home, score_away, "
                   "       result, league, kickoff, op_1x2_h, op_1x2_d, op_1x2_a, "
                   "       is_virtual "
                   "FROM match_outcomes "
                   "WHERE op_1x2_h IS NOT NULL AND op_1x2_d IS NOT NULL "
                   "  AND op_1x2_a IS NOT NULL "
                   "  AND score_home IS NOT NULL AND score_away IS NOT NULL "
                   "  AND COALESCE(is_virtual, 0) = 0")
        oc = pd.read_sql_query(out_sql, conn)
    finally:
        conn.close()

    oc["match_key"] = oc["home"].astype(str) + " vs " + oc["away"].astype(str)
    # 先重命名 op 防止覆盖
    op = op.rename(columns={"home": "op_home", "draw": "op_draw", "away": "op_away"})
    df = op.merge(oc, on="match_key", how="inner")
    # 去除虚拟联赛（电子盘 8 分钟）
    df = df[df["is_virtual"] == 0]
    log.info(f"[OLPO] 开盘价+赛果联表: {len(df)} 场")
    return df


# ────────────────────────────────────────────────────────────────────────────
# 交叉验证: football_data.db (312K) 用于 events.db 单点结果的稳健性验证
# ────────────────────────────────────────────────────────────────────────────

def cross_validate_with_history(verbose: bool = True) -> dict:
    """用 312K 历史开盘赔率验证 events.db 的 Layer 1 结论。

    这是防回归核心 —— events.db 单点发现曾出现 draw ROI +8.92% 假信号 (与
    312K 历史的 -10.17% 矛盾), 必须先通过本验证才能进入下游应用。
    """
    db = sqlite3.connect(os.path.join(ROOT, "data", "football_data.db"))
    try:
        df = pd.read_sql_query("""
            SELECT open_home_odds, open_draw_odds, open_away_odds, final_result
            FROM historical_matches
            WHERE open_home_odds IS NOT NULL AND open_draw_odds IS NOT NULL
              AND open_away_odds IS NOT NULL AND final_result IS NOT NULL
              AND open_home_odds > 1.01 AND open_draw_odds > 1.01
              AND open_away_odds > 1.01
        """, db)
    finally:
        db.close()

    if not len(df):
        return {"n": 0}

    inv_h = 1.0 / df["open_home_odds"]
    inv_d = 1.0 / df["open_draw_odds"]
    inv_a = 1.0 / df["open_away_odds"]
    total = inv_h + inv_d + inv_a
    p_h = inv_h / total
    p_d = inv_d / total
    p_a = inv_a / total

    home_win = (df["final_result"] == "H").astype(int).values
    draw_win = (df["final_result"] == "D").astype(int).values
    away_win = (df["final_result"] == "A").astype(int).values

    def roi(y, odds):
        payout = np.where(y == 1, odds.values - 1.0, -1.0)
        return float(payout.mean())

    res = {
        "source": "football_data.db.historical_matches",
        "n": int(len(df)),
        "home_roi_blind": round(roi(home_win, df["open_home_odds"]), 4),
        "draw_roi_blind": round(roi(draw_win, df["open_draw_odds"]), 4),
        "away_roi_blind": round(roi(away_win, df["open_away_odds"]), 4),
        "home_actual_win_rate": round(float(home_win.mean()), 4),
        "draw_actual_rate": round(float(draw_win.mean()), 4),
        "away_actual_win_rate": round(float(away_win.mean()), 4),
        "home_market_p": round(float(p_h.mean()), 4),
        "draw_market_p": round(float(p_d.mean()), 4),
        "away_market_p": round(float(p_a.mean()), 4),
    }
    res["no_fake_edge"] = (res["home_roi_blind"] < 0 and
                           res["draw_roi_blind"] < 0 and
                           res["away_roi_blind"] < 0)

    if verbose:
        print("=" * 70)
        print("交叉验证 — football_data.db 312K 历史")
        print("=" * 70)
        print(f"  样本: {res['n']} 场")
        print()
        print(f"  {'方向':<6} {'开盘隐含':<10} {'实际':<10} {'偏差':<8} {'盲投ROI':<8}")
        for side, p, a, roi in [
            ("主", res["home_market_p"], res["home_actual_win_rate"], res["home_roi_blind"]),
            ("平", res["draw_market_p"], res["draw_actual_rate"], res["draw_roi_blind"]),
            ("客", res["away_market_p"], res["away_actual_win_rate"], res["away_roi_blind"]),
        ]:
            print(f"  {side:<6} {p:<10.2%} {a:<10.2%} {a-p:+.4f}   {roi:+.2%}")
        print()
        print(f"  无假 edge: {res['no_fake_edge']}")
        if res["no_fake_edge"]:
            print(f"  结论: 开盘价健康校准 (三方 ROI 都 < 0)")
    return res


# ────────────────────────────────────────────────────────────────────────────
# Layer 1 — 开盘价校准度
# ────────────────────────────────────────────────────────────────────────────

def opening_calibration(db_path: str | None = None,
                       n_bins: int = 10,
                       verbose: bool = True) -> dict:
    """Layer 1: 开盘隐含概率的校准度。

    健康标志:
      - 三方 Brier 都接近 0 (开盘价校准好)
      - 校准斜率 ≈ 1 (预测概率与实际频率一致)
      - 盲投 ROI 三方都为负 (无假 edge)

    ⚠️ 重要: 单一数据源 ROI 可能受数据污染影响 (2026-08-11 events.db draw ROI +8.92%
        vs football_data.db draw ROI -10.17% 矛盾已证伪)。务必 cross-validate。
    """
    df = _load_opening_with_outcome(db_path)
    if not len(df):
        return {"n": 0}

    # 三个方向的 binary outcome: 该方向胜 = 1, 否则 = 0
    home_win = (df["result"] == "home").astype(int).values
    draw     = (df["result"] == "draw").astype(int).values
    away_win = (df["result"] == "away").astype(int).values

    p_home = df["p_home_devig"].values
    p_draw = df["p_draw_devig"].values
    p_away = df["p_away_devig"].values

    # Brier 分数 (越小越好, 0 = 完美)
    brier_home = float(((p_home - home_win) ** 2).mean())
    brier_draw = float(((p_draw - draw) ** 2).mean())
    brier_away = float(((p_away - away_win) ** 2).mean())
    # Naive baseline (用全局均值预测): Brier_naive = 2 * p * (1-p)
    naive_brier = float((df["result"].value_counts(normalize=True).max() *
                        (1 - df["result"].value_counts(normalize=True).max())))

    # 校准斜率: 实际胜率 = a * 开盘概率 + b;  a=1 完美
    def _calib_slope(p: np.ndarray, y: np.ndarray) -> float:
        if p.std() < 1e-9 or len(p) < 10:
            return float("nan")
        return float(np.cov(p, y, ddof=0)[0, 1] / np.var(p))

    # ECE: 分箱后 |预测均值 - 实际频率| 的加权平均
    def _ece(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> float:
        bins = np.linspace(0, 1, n_bins + 1)
        ece = 0.0
        for lo, hi in zip(bins[:-1], bins[1:]):
            mask = (p >= lo) & (p < hi)
            if mask.sum() == 0:
                continue
            ece += mask.mean() * abs(p[mask].mean() - y[mask].mean())
        return float(ece)

    # 盲投 ROI: 假设每场都投该方向 1 单位
    def _roi_blind(p: np.ndarray, y: np.ndarray, odds_col: pd.Series) -> float:
        # 实际命中时赚 (odds-1), 未命中亏 1
        payout = np.where(y == 1, odds_col.values - 1.0, -1.0)
        return float(payout.mean())

    odds_h = df["op_home"].values  # 原始开盘赔率
    odds_d = df["op_draw"].values
    odds_a = df["op_away"].values

    res = {
        "n": int(len(df)),
        "avg_overround": round(float(df["overround"].mean()), 4),
        # 主胜方向
        "home_brier": round(brier_home, 4),
        "home_ece":   round(_ece(p_home, home_win, n_bins), 4),
        "home_calib_slope": round(_calib_slope(p_home, home_win), 3),
        "home_actual_win_rate": round(float(home_win.mean()), 4),
        "home_market_p": round(float(p_home.mean()), 4),
        "home_roi_blind": round(_roi_blind(p_home, home_win, df["op_home"]), 4),
        # 平局
        "draw_brier": round(brier_draw, 4),
        "draw_ece":   round(_ece(p_draw, draw, n_bins), 4),
        "draw_calib_slope": round(_calib_slope(p_draw, draw), 3),
        "draw_actual_rate": round(float(draw.mean()), 4),
        "draw_market_p": round(float(p_draw.mean()), 4),
        "draw_roi_blind": round(_roi_blind(p_draw, draw, df["op_draw"]), 4),
        # 客胜
        "away_brier": round(brier_away, 4),
        "away_ece":   round(_ece(p_away, away_win, n_bins), 4),
        "away_calib_slope": round(_calib_slope(p_away, away_win), 3),
        "away_actual_win_rate": round(float(away_win.mean()), 4),
        "away_market_p": round(float(p_away.mean()), 4),
        "away_roi_blind": round(_roi_blind(p_away, away_win, df["op_away"]), 4),
    }
    # 全局平均 Brier (用作基准对比)
    res["mean_brier"] = round((brier_home + brier_draw + brier_away) / 3, 4)
    res["brier_break_even"] = round(2 * res["avg_overround"], 4)
    # 判据: 三方 ROI 都为负 = 无假 edge
    res["no_fake_edge"] = (res["home_roi_blind"] < 0 and
                           res["draw_roi_blind"] < 0 and
                           res["away_roi_blind"] < 0)

    if verbose:
        print("=" * 70)
        print("Layer 1 — 开盘价校准度 (Opening-Line Calibration)")
        print("=" * 70)
        print(f"  样本          : {res['n']} 场")
        print(f"  平均抽水      : {res['avg_overround']:.2%}")
        print()
        print(f"  {'方向':<6} {'开盘隐含':<10} {'实际胜率':<10} {'偏差':<8} "
              f"{'Brier':<8} {'ECE':<8} {'斜率':<6} {'盲投ROI':<8}")
        for side, p, a, br, ece, sl, roi in [
            ("主", res["home_market_p"], res["home_actual_win_rate"],
             res["home_brier"], res["home_ece"], res["home_calib_slope"], res["home_roi_blind"]),
            ("平", res["draw_market_p"], res["draw_actual_rate"],
             res["draw_brier"], res["draw_ece"], res["draw_calib_slope"], res["draw_roi_blind"]),
            ("客", res["away_market_p"], res["away_actual_win_rate"],
             res["away_brier"], res["away_ece"], res["away_calib_slope"], res["away_roi_blind"]),
        ]:
            print(f"  {side:<6} {p:<10.2%} {a:<10.2%} {a-p:+.4f}   "
                  f"{br:<8.4f} {ece:<8.4f} {sl:<6.3f} {roi:+.2%}")
        print()
        print(f"  平均 Brier    : {res['mean_brier']:.4f}  (基准 {res['brier_break_even']:.4f})")
        print(f"  无假 edge     : {res['no_fake_edge']}  (三方 ROI 都 < 0)")

        if not res["no_fake_edge"]:
            print()
            print("  ⚠️  存在假 edge: 开盘价提取或赛果数据有问题, 需排查。")
        else:
            # 找出系统性偏差方向
            biases = []
            for side, p, a in [("主", res["home_market_p"], res["home_actual_win_rate"]),
                                ("平", res["draw_market_p"], res["draw_actual_rate"]),
                                ("客", res["away_market_p"], res["away_actual_win_rate"])]:
                gap = a - p
                if abs(gap) > 0.01:
                    direction = "高估" if gap < 0 else "低估"
                    biases.append(f"{side}侧{direction}{abs(gap):.2%}")
            if biases:
                print(f"  系统性偏差    : {' / '.join(biases)}")
                print(f"  含义: 开盘价在{'/'.join(b.split('侧')[0] for b in biases)}方向上"
                      f"{'低估' if any('低估' in b for b in biases) else '高估'}实际胜率。")
                print(f"  这是结构性的 —— 开盘价校准参考 (不是信号, 但是边缘机会的入口)。")
    return res


# ────────────────────────────────────────────────────────────────────────────
# Layer 2 — 开盘价方向一致性
# ────────────────────────────────────────────────────────────────────────────

def opening_direction_alignment(db_path: str | None = None,
                                verbose: bool = True) -> dict:
    """Layer 2: 开盘价暗示的"最可能方向"命中率。

    对每场比赛, 取开盘隐含概率最高的方向作为开盘"暗示", 看实际命中率。
    与naive baseline (= max(p_h, p_d, p_a) 平均) 对比。
    """
    df = _load_opening_with_outcome(db_path)
    if not len(df):
        return {"n": 0}

    # 开盘暗示的方向
    p = df[["p_home_devig", "p_draw_devig", "p_away_devig"]].values
    implied_idx = p.argmax(axis=1)
    implied_dir = np.where(implied_idx == 0, "home",
                np.where(implied_idx == 1, "draw", "away"))
    implied_p = p.max(axis=1)

    actual_dir = df["result"].values
    hit = (implied_dir == actual_dir).astype(int)

    res = {
        "n": int(len(df)),
        "hit_rate_overall": round(float(hit.mean()), 4),
        "implied_p_mean": round(float(implied_p.mean()), 4),
        # 按开盘概率分箱
        "bins": []
    }
    # 分桶看命中率与开盘概率是否一致
    for lo, hi in [(0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 1.00)]:
        mask = (implied_p >= lo) & (implied_p < hi)
        if mask.sum() == 0:
            continue
        res["bins"].append({
            "bin": f"[{lo:.2f}, {hi:.2f})",
            "n": int(mask.sum()),
            "implied_p_mean": round(float(implied_p[mask].mean()), 4),
            "actual_hit_rate": round(float(hit[mask].mean()), 4),
            "gap": round(float(hit[mask].mean() - implied_p[mask].mean()), 4),
        })

    if verbose:
        print("=" * 70)
        print("Layer 2 — 开盘价方向一致性 (Direction Alignment)")
        print("=" * 70)
        print(f"  总命中率      : {res['hit_rate_overall']:.2%}  "
              f"(开盘平均暗示概率 {res['implied_p_mean']:.2%})")
        print()
        print(f"  {'开盘概率桶':<16} {'样本':<6} {'平均开盘':<10} {'实际命中':<10} {'偏差':<8}")
        for b in res["bins"]:
            print(f"  {b['bin']:<16} {b['n']:<6} {b['implied_p_mean']:<10.2%} "
                  f"{b['actual_hit_rate']:<10.2%} {b['gap']:+.2%}")
        print()
        # 关键判据: 高暗示概率桶 (>0.60) 的实际命中率是否显著低于暗示概率
        high_bin = [b for b in res["bins"] if b["implied_p_mean"] >= 0.60]
        if high_bin:
            actual = np.mean([b["actual_hit_rate"] for b in high_bin])
            implied = np.mean([b["implied_p_mean"] for b in high_bin])
            print(f"  高暗示桶(>0.60): 开盘隐含 {implied:.2%} vs 实际命中 {actual:.2%}")
            if actual < implied - 0.03:
                print(f"  ⚠️  高暗示桶命中率偏低, 即开盘价过于乐观 (favorite 倾向被市场高估)")
            elif actual > implied + 0.03:
                print(f"  ⚠️  高暗示桶命中率偏高, 即开盘价过于悲观 (favorite 倾向被市场低估)")
            else:
                print(f"  健康: 高暗示桶命中率与开盘价一致")
    return res


# ────────────────────────────────────────────────────────────────────────────
# Layer 3 — 反事实价值场次 (upset patterns)
# ────────────────────────────────────────────────────────────────────────────

def upset_patterns(db_path: str | None = None,
                   min_fav_prob: float = 0.65,
                   verbose: bool = True) -> pd.DataFrame:
    """Layer 3: 开盘强烈暗示某方向、但实际颠覆的场次。

    Args:
        min_fav_prob: 开盘最强方向的最低隐含概率 (默认 0.65 = 强信号)
    """
    df = _load_opening_with_outcome(db_path)
    if not len(df):
        return pd.DataFrame()

    p = df[["p_home_devig", "p_draw_devig", "p_away_devig"]].values
    implied_idx = p.argmax(axis=1)
    implied_dir = np.where(implied_idx == 0, "home",
                np.where(implied_idx == 1, "draw", "away"))
    implied_p = p.max(axis=1)

    actual_dir = df["result"].values
    is_upset = (implied_dir != actual_dir) & (implied_p >= min_fav_prob)

    df = df.copy()
    df["implied_dir"] = implied_dir
    df["implied_p"] = implied_p
    df["is_upset"] = is_upset
    df["upset_target"] = np.where(is_upset, actual_dir, "")
    df["upset_target_p"] = np.where(
        is_upset,
        np.where(implied_idx == 0, p[:, 1],  # draw
        np.where(implied_idx == 1, p[:, 2], p[:, 0])),  # away : home
        0
    )

    if verbose:
        print("=" * 70)
        print(f"Layer 3 — 反事实价值场次 (开盘隐含≥{min_fav_prob:.0%} 的颠覆)")
        print("=" * 70)
        total_strong = int((implied_p >= min_fav_prob).sum())
        n_upset = int(is_upset.sum())
        print(f"  强信号场次    : {total_strong}")
        print(f"  颠覆场次      : {n_upset}  ({n_upset/max(total_strong,1):.2%})")
        print()
        if n_upset >= 10:
            # 按联赛统计
            league_up = df[is_upset].groupby("league").size().sort_values(ascending=False)
            print(f"  Top 10 颠覆联赛:")
            for lg, n in league_up.head(10).items():
                print(f"    {str(lg)[:30]:<30} : {n} 场")
            print()
            # 按"被颠覆方向"统计
            tgt_up = df[is_upset]["upset_target"].value_counts()
            print(f"  被颠覆方向分布:")
            for d, n in tgt_up.items():
                print(f"    实际{d}: {n} 场")
            print()
            # 赔率特征: 颠覆场次的开盘隐含 vs 实际胜率方向
            print(f"  颠覆场次的开盘隐含概率(被颠覆侧):")
            tgt_probs = df[is_upset]["upset_target_p"]
            print(f"    均值 {tgt_probs.mean():.2%} / 中位 {tgt_probs.median():.2%} / "
                  f"最大 {tgt_probs.max():.2%}")
            print(f"  (含义: 这些场次里, 开盘价低估了被颠覆侧的概率 —— 颠覆方原本有"
                  f"{tgt_probs.mean():.0%} 的隐藏实力)")
        else:
            print("  颠覆场次过少 (<10), 暂无可用模式")

    return df[is_upset].sort_values("implied_p", ascending=False)


# ────────────────────────────────────────────────────────────────────────────
# CLI — 三层一站体检
# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, ROOT)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    print()
    print("═" * 70)
    print("  开盘价预判分析 (OLPO) — 三层一站体检")
    print("═" * 70)
    print()

    cal = opening_calibration()
    print()
    opening_direction_alignment()
    print()
    cross_validate_with_history()
    print()
    upset_patterns()