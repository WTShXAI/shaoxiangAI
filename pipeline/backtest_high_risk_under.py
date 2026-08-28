"""
哨响AI — HIGH risk 小球策略回测 v1.0
=====================================
验证: 在模板偏差检测器标记为 HIGH risk 的比赛中, 全选小球能否盈利?

数据源:
    template_deviation_scan.csv
  + pipeline.clean_outcomes.load_clean_outcomes()   (剔虚拟盘 + 剔采集截断)
  + pipeline.opening_line.build_opening_lines()     (真·初盘主盘线)

⚠ 2026-08-05 数据保真事故修复:
    旧版直接 read_sql match_outcomes, 吃进 228 场电子盘 + 1307 场采集截断
    (半场缺失导致终场比分被冻结, 均进球 1.95 / 零封 29%), 并使用
    match_outcomes.op_ou_line —— 那是"梯队最小线"而非主盘线, 制造出
    "无脑买大 +15% ROI" 的假 edge。现全部改走 SSoT。
"""

import os, sys, json
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
GQ_DB = os.path.join(DATA_DIR, "events.db")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from pipeline.clean_outcomes import load_clean_outcomes   # noqa: E402
from pipeline.opening_line import build_opening_lines     # noqa: E402


def load_data():
    """加载偏差评分 + 干净赛果 + 真初盘主盘线."""
    scan = pd.read_csv(os.path.join(DATA_DIR, "pricing_template", "template_deviation_scan.csv"))

    outcomes, rep = load_clean_outcomes(return_report=True)
    print(f"[数据] 赛果清洗: {rep['before']['n']} -> {rep['after']['n']} 场 "
          f"(剔虚拟 {rep['dropped_virtual']} / 剔截断 {rep['dropped_truncated']})")

    # 真·初盘主盘线 (替代 match_outcomes.op_ou_line 的最小线)
    # 先把 match_outcomes 里那套错误的 op_ou_* 彻底丢掉, 避免同名列共存
    outcomes = outcomes.drop(columns=[c for c in
                                      ("op_ou_line", "op_ou_over", "op_ou_under")
                                      if c in outcomes.columns])
    op = build_opening_lines()
    outcomes["match_key"] = outcomes["home"].astype(str) + " vs " + outcomes["away"].astype(str)
    outcomes = outcomes.merge(
        op[["match_key", "line", "over", "under", "overround", "p_over_devig"]],
        on="match_key", how="left",
    ).drop_duplicates(subset=["mid"])
    outcomes = outcomes.rename(columns={
        "line": "op_ou_line", "over": "op_ou_over", "under": "op_ou_under",
    })
    print(f"[数据] 初盘主盘线匹配: {outcomes['op_ou_line'].notna().sum()}/{len(outcomes)} 场")

    outcomes["mid"] = outcomes["mid"].astype(str)
    scan["mid"] = scan["mid"].astype(str)
    keep = ["mid", "score_home", "score_away", "result",
            "op_ou_line", "op_ou_over", "op_ou_under", "overround", "p_over_devig"]
    keep = [c for c in keep if c in outcomes.columns]
    df = scan.merge(outcomes[keep], on="mid", how="inner")
    df["total_goals"] = df["score_home"] + df["score_away"]
    return df


def backtest_under_strategy(df: pd.DataFrame):
    """
    策略: HIGH risk (>0.6) + 有 OU 数据 → 下注小球
    计算: 每注1单位的ROI
    """
    print("=" * 70)
    print("HIGH risk 小球策略回测")
    print("=" * 70)

    # 筛选: HIGH risk + 有OU开盘
    high = df[(df["template_risk_score"] > 0.5) &
              df["op_ou_over"].notna() &
              df["op_ou_under"].notna()].copy()

    if high.empty:
        print("无符合条件的比赛!")
        return

    layer_roi = {}

    print(f"\n总场次: {len(high)}")

    # 按风险分层
    for risk_level, label in [(0.6, "HIGH"), (0.5, "MEDIUM+")]:
        sub = high[high["template_risk_score"] > risk_level]
        if len(sub) == 0:
            continue

        # OU 结果判定
        results = []
        for _, r in sub.iterrows():
            line = float(r["op_ou_line"])
            total = int(r["total_goals"])
            ou_under_odds = float(r["op_ou_under"])

            if total < line:
                profit = ou_under_odds - 1  # 赢
            elif total > line:
                profit = -1  # 输
            else:
                profit = 0  # 走水

            results.append({
                "match_id": r["mid"],
                "home": r["home"],
                "away": r["away"],
                "league": r["league"],
                "ou_line": line,
                "total_goals": total,
                "ou_result": "under" if total < line else "over" if total > line else "push",
                "odds": ou_under_odds,
                "profit": profit,
                "risk_score": r["template_risk_score"],
                "risk_cat": classify_risk_detail(r["league"]),
            })

        res_df = pd.DataFrame(results)
        n = len(res_df)
        wins = (res_df["profit"] > 0).sum()
        losses = (res_df["profit"] < 0).sum()
        pushes = (res_df["profit"] == 0).sum()
        total_profit = res_df["profit"].sum()
        roi = total_profit / n
        hit_rate = wins / (wins + losses) if (wins + losses) > 0 else 0
        layer_roi[f"{label}(>{risk_level})"] = roi

        print(f"\n{'='*50}")
        print(f"{label} risk (>{risk_level}), n={n}")
        print(f"{'='*50}")
        print(f"  赢: {wins}, 输: {losses}, 走水: {pushes}")
        print(f"  命中率(去走水): {hit_rate:.1%}")
        print(f"  总利润: {total_profit:+.2f}u")
        print(f"  ROI: {roi:+.2%}")
        print(f"  平均赔率: {res_df['odds'].mean():.2f}")

        # 按赛事类型分层
        print(f"\n  按赛事类型分层:")
        for cat in res_df["risk_cat"].unique():
            cat_df = res_df[res_df["risk_cat"] == cat]
            if len(cat_df) < 3:
                continue
            c_profit = cat_df["profit"].sum()
            c_roi = c_profit / len(cat_df)
            c_hit = (cat_df["profit"] > 0).sum() / max(1, (cat_df["profit"] > 0).sum() + (cat_df["profit"] < 0).sum())
            print(f"    {cat:20s}: n={len(cat_df):3d}, hit={c_hit:.1%}, ROI={c_roi:+.2%}")

        # 按OU盘口线分层
        print(f"\n  按OU盘口线分层:")
        for line_val in sorted(res_df["ou_line"].unique()):
            line_df = res_df[res_df["ou_line"] == line_val]
            if len(line_df) < 3:
                continue
            l_profit = line_df["profit"].sum()
            l_roi = l_profit / len(line_df)
            l_hit = (line_df["profit"] > 0).sum() / max(1, (line_df["profit"] > 0).sum() + (line_df["profit"] < 0).sum())
            print(f"    OU{line_val:.1f}: n={len(line_df):3d}, hit={l_hit:.1%}, ROI={l_roi:+.2%}")

        # 按偏差信号分层 (跨市场信号数)
        if "L2_cross_market_signals" in sub.columns:
            print(f"\n  按跨市场信号数分层:")
            for sig_level in [0, 1, 2, 3]:
                mask = sub["L2_cross_market_signals"] >= sig_level
                sig_idx = sub[mask].index
                sig_df2 = res_df[res_df.index.isin(sig_idx)]
                if len(sig_df2) < 3:
                    continue
                s_profit = sig_df2["profit"].sum()
                s_roi = s_profit / len(sig_df2)
                s_hit = (sig_df2["profit"] > 0).sum() / max(1, (sig_df2["profit"] > 0).sum() + (sig_df2["profit"] < 0).sum())
                print(f"    信号≥{sig_level}: n={len(sig_df2):3d}, hit={s_hit:.1%}, ROI={s_roi:+.2%}")

    # 对比: 随机选球的基准
    print(f"\n{'='*50}")
    print(f"基准对照: 全部有OU的比赛 (全选小球)")
    all_ou = df[df["op_ou_over"].notna()].copy()
    all_results = []
    for _, r in all_ou.iterrows():
        line = float(r["op_ou_line"])
        total = int(r["total_goals"])
        odds = float(r["op_ou_under"])
        if total < line:
            profit = odds - 1
        elif total > line:
            profit = -1
        else:
            profit = 0
        all_results.append(profit)

    all_profit = sum(all_results)
    all_roi = all_profit / len(all_results)
    all_hit = sum(1 for p in all_results if p > 0) / max(1, sum(1 for p in all_results if p != 0))
    print(f"  n={len(all_results)}, hit={all_hit:.1%}, ROI={all_roi:+.2%}")
    print(f"  (健康标志: 该基准应 ≈ -抽水, 约 -5%~0%。若显著为正 -> 盘口提取或赛果有污染)")
    # 注: 原实现在此处用循环残留的 roi, 会把最后一档(MEDIUM+)误标成 HIGH。
    for lbl, r_ in layer_roi.items():
        print(f"  {lbl} 相对基准提升: {r_ - all_roi:+.2%}")

    return res_df if 'res_df' in dir() else None


def classify_risk_detail(league: str) -> str:
    """细分赛事类型."""
    name = str(league).lower()
    if any(k in name for k in ["u23", "u21", "u20", "u19", "u18", "u17", "青年", "预备", "reserve"]):
        return "youth"
    if any(k in name for k in ["友谊", "friendly", "热身"]):
        return "friendly"
    if any(k in name for k in ["杯", "cup"]):
        return "cup"
    if any(k in name for k in ["非洲", "africa", "caf", "kagame", "cecafa"]):
        return "african"
    if any(k in name for k in ["女子", "women", "(女)"]):
        return "women"
    if any(k in name for k in ["资格", "qualify", "预选"]):
        return "qualifier"
    return "other"


if __name__ == "__main__":
    df = load_data()
    print(f"加载: {len(df)} 场比赛 (有偏差评分 + 赛果)")

    # 基础统计
    print(f"\n基础统计:")
    print(f"  总场次: {len(df)}")
    print(f"  有OU数据: {df['op_ou_over'].notna().sum()}")
    print(f"  场均进球: {df['total_goals'].mean():.2f}")
    print(f"  大球率(>2.5): {(df['total_goals'] > 2.5).mean():.1%}")

    # 回测
    backtest_under_strategy(df)
