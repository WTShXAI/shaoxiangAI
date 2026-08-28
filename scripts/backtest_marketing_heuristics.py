#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测微信营销号"盘口三定律"与"2-3-4球区间"策略。
数据源: events.db match_outcomes (08-05 已回填 OU 主盘线 / AH 已剥离)。
原则: 只使用清洗后数据; 命中报告必须并排朴素基线。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "data" / "events.db"
REPORT_PATH = ROOT / "data" / "marketing_heuristics_backtest.json"

# 清洗过滤常量
VIRTUAL_LEAGUE_KEYWORDS = ("8分钟", "友谊", "电子", "EASports", "EAFC", "PANDA")
AH_CORRUPTION_EPOCH = 1784325860  # 07-18 06:04 后 AH line=0 全伪造


def load_clean_outcomes() -> list[dict[str, Any]]:
    """加载已完赛、有干净 OU 主盘线、非虚拟/非截断的比赛。

    使用 SSoT 入口:
      - pipeline.clean_outcomes.load_clean_outcomes()  剔虚拟赛事 + 采集截断
      - pipeline.opening_line.build_opening_lines()    重建真·初盘主线
    避免直接读 match_outcomes.op_ou_line/op_ou_over 中的残留离谱线污染。
    """
    from pipeline.clean_outcomes import load_clean_outcomes as _co
    from pipeline.opening_line import build_opening_lines

    oc = _co(db_path=str(DB_PATH), drop_virtual=True, drop_truncated=True)
    op = build_opening_lines(db_path=str(DB_PATH), market="OU", full_time_only=True)

    oc["match_key"] = oc["home"].astype(str) + " vs " + oc["away"].astype(str)
    oc["tot"] = oc["score_home"] + oc["score_away"]

    df = op.merge(
        oc[["match_key", "tot", "league", "score_home", "score_away",
            "op_ah_line", "op_ah_home", "op_ah_away", "op_1x2_h", "op_1x2_d", "op_1x2_a"]],
        on="match_key", how="inner"
    )
    df = df.drop_duplicates(subset=["match_key"])

    # 仅保留合理主盘线: 全场 OU 线应在 1.5~4.5 之间
    df = df[(df["line"] >= 1.5) & (df["line"] <= 4.5)].copy()

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "match_key": r["match_key"],
            "league": r["league"],
            "score_home": int(r["score_home"]),
            "score_away": int(r["score_away"]),
            "op_ou_line": float(r["line"]),
            "op_ou_over": float(r["over"]),
            "op_ou_under": float(r["under"]),
            "op_ah_line": r["op_ah_line"],
            "op_ah_home": r["op_ah_home"],
            "op_ah_away": r["op_ah_away"],
            "op_1x2_h": r["op_1x2_h"],
            "op_1x2_d": r["op_1x2_d"],
            "op_1x2_a": r["op_1x2_a"],
        })
    return rows


def total_goals(r: dict) -> int:
    return int(r["score_home"] + r["score_away"])


def ou_result(r: dict) -> str:
    """大球/小球结果。"""
    return "over" if total_goals(r) > r["op_ou_line"] else "under"


def over_under_push(r: dict) -> tuple[str, float]:
    """
    考虑亚洲盘走水/半赢的 OU 结果与净赢率。
    返回 (side, profit_per_unit): side 是 'over' 或 'under'。
    """
    line = r["op_ou_line"]
    total = total_goals(r)
    over_odds = r["op_ou_over"]
    under_odds = r["op_ou_under"]

    # 亚洲盘常见线处理: 2.25, 2.5, 2.75, 3 等
    frac = line - int(line)
    base = int(line)

    if frac == 0.0:
        # 整数线: 走水
        if total == line:
            return "push", 0.0
        return ("over", over_odds - 1.0) if total > line else ("under", under_odds - 1.0)
    elif frac == 0.25:
        # x.25: 一半押 x，一半押 x+0.5
        if total > base + 0.5:
            return "over", over_odds - 1.0
        if total < base:
            return "under", under_odds - 1.0
        if total == base:
            return "under", (under_odds - 1.0) * 0.5  # 一半赢一半走水 => 另一半 over 输
        # total == base+1 (> base+0.5): over 全赢，已覆盖
    elif frac == 0.5:
        if total > line:
            return "over", over_odds - 1.0
        return "under", under_odds - 1.0
    elif frac == 0.75:
        # x.75: 一半押 x+0.5，一半押 x+1
        if total > base + 1:
            return "over", over_odds - 1.0
        if total < base + 0.5:
            return "under", under_odds - 1.0
        if total == base + 1:
            return "over", (over_odds - 1.0) * 0.5
    # 简单 fallback: 按标准 over/under 判定，无走水
    return ("over", over_odds - 1.0) if total > line else ("under", under_odds - 1.0)


def strategy_2_3_goals(rows: list[dict]) -> dict:
    """
    "2-3球"策略: 盘口 2.25/2.5, 大球水位 <=0.95。
    报告: 总进球为2或3的命中率; 若按大球投注的 ROI。
    """
    # "水位0.95" = 利润赔率 0.95, 即 decimal odds <= 1.95
    sel = [r for r in rows if r["op_ou_line"] in (2.25, 2.5) and r["op_ou_over"] <= 1.95]
    if not sel:
        return {"n": 0, "note": "无样本"}

    hits_23 = sum(1 for r in sel if total_goals(r) in (2, 3))
    # 大球投注收益 (亚洲盘)
    profits = []
    for r in sel:
        side, profit = over_under_push(r)
        # 营销号策略押"2-3球"不是纯大球; 这里同时看大球 ROI 作为参照
        if side == "push":
            profits.append(0.0)
        elif side == "over":
            profits.append(profit)
        else:
            profits.append(-1.0)

    return {
        "n": len(sel),
        "hit_2_or_3": hits_23,
        "hit_rate_2_or_3": round(hits_23 / len(sel), 4),
        "avg_total": round(sum(total_goals(r) for r in sel) / len(sel), 3),
        "over_bet_roi": round(sum(profits) / len(sel), 4),
        "over_bet_win": sum(1 for p in profits if p > 0),
        "baseline_all_2_or_3": round(
            sum(1 for r in rows if total_goals(r) in (2, 3)) / len(rows), 4
        ),
    }


def strategy_3_4_goals(rows: list[dict]) -> dict:
    """
    "3-4球"策略: 盘口 >=2.5 (最好 2.75), 大球水位 <=0.90。
    """
    # "水位0.90" = decimal odds <= 1.90
    sel = [r for r in rows if r["op_ou_line"] >= 2.5 and r["op_ou_over"] <= 1.90]
    sel_275 = [r for r in sel if r["op_ou_line"] == 2.75]
    if not sel:
        return {"n": 0, "note": "无样本"}

    def report(sub: list[dict], label: str) -> dict:
        hits_34 = sum(1 for r in sub if total_goals(r) in (3, 4))
        profits = []
        for r in sub:
            side, profit = over_under_push(r)
            if side == "push":
                profits.append(0.0)
            elif side == "over":
                profits.append(profit)
            else:
                profits.append(-1.0)
        return {
            "label": label,
            "n": len(sub),
            "hit_3_or_4": hits_34,
            "hit_rate_3_or_4": round(hits_34 / len(sub), 4),
            "avg_total": round(sum(total_goals(r) for r in sub) / len(sub), 3),
            "over_bet_roi": round(sum(profits) / len(sub), 4),
            "over_bet_win": sum(1 for p in profits if p > 0),
        }

    return {
        "line_ge_2_5": report(sel, "line>=2.5 & over<=0.90"),
        "line_eq_2_75": report(sel_275, "line=2.75 & over<=0.90") if sel_275 else {"n": 0},
        "baseline_all_3_or_4": round(
            sum(1 for r in rows if total_goals(r) in (3, 4)) / len(rows), 4
        ),
    }


def strategy_water_extreme(rows: list[dict]) -> dict:
    """
    定律三: 超低水 <0.75 / 超高水 >1.10。
    深盘+超低水 = 高危诱盘(押大球危险); 浅盘+超高水 = 可能阻下(押小球危险)。
    """
    # "超低水<0.75" = decimal odds < 1.75; "超高水>1.10" = decimal odds > 2.10
    low_water = [r for r in rows if r["op_ou_over"] < 1.75]
    high_water = [r for r in rows if r["op_ou_over"] > 2.10]
    deep_low = [r for r in low_water if r["op_ou_line"] >= 2.75]
    shallow_high = [r for r in high_water if r["op_ou_line"] <= 2.25]

    def ou_roi(sub: list[dict], bet_side: str) -> dict:
        if not sub:
            return {"n": 0}
        profits = []
        wins = 0
        for r in sub:
            side, profit = over_under_push(r)
            if side == "push":
                profits.append(0.0)
            elif side == bet_side:
                profits.append(profit)
                wins += 1
            else:
                profits.append(-1.0)
        return {
            "n": len(sub),
            "bet": bet_side,
            "win": wins,
            "win_rate": round(wins / len(sub), 4),
            "roi": round(sum(profits) / len(sub), 4),
        }

    return {
        "low_water_over_lt_0_75": {
            "n": len(low_water),
            "avg_line": round(sum(r["op_ou_line"] for r in low_water) / len(low_water), 3) if low_water else None,
            "over_roi": ou_roi(low_water, "over"),
            "under_roi": ou_roi(low_water, "under"),
        },
        "high_water_over_gt_1_10": {
            "n": len(high_water),
            "avg_line": round(sum(r["op_ou_line"] for r in high_water) / len(high_water), 3) if high_water else None,
            "over_roi": ou_roi(high_water, "over"),
            "under_roi": ou_roi(high_water, "under"),
        },
        "deep_low_water": {
            "n": len(deep_low),
            "over_roi": ou_roi(deep_low, "over"),
            "under_roi": ou_roi(deep_low, "under"),
        },
        "shallow_high_water": {
            "n": len(shallow_high),
            "over_roi": ou_roi(shallow_high, "over"),
            "under_roi": ou_roi(shallow_high, "under"),
        },
    }


def strategy_line_deviation_ah(rows: list[dict]) -> dict:
    """
    定律一: 实力差定初盘，偏差>0.25球。
    用 1X2 去水概率换算理论让球，对比实际 AH 让球线。
    注意: AH 已严重污染，真实非零 AH 样本极少，结果仅作参考。
    """
    import math
    clean_ah = []
    for r in rows:
        line = r.get("op_ah_line")
        if line is None or (isinstance(line, float) and math.isnan(line)) or float(line) == 0.0:
            continue
        clean_ah.append(r)

    if len(clean_ah) < 100:
        return {"n": len(clean_ah), "note": "有效 AH 样本不足，无法回测定律一"}

    def fair_handicap(h: float, d: float, a: float) -> float | None:
        """用 1X2 去水概率粗算公平让球: 主队胜率 - 客队胜率，再经验缩放。"""
        s = h + d + a
        if s < 0.99 or s > 1.01:
            return None
        ph, pa = h / s, a / s
        # 经验: 胜率差 10pp ≈ 0.25 球
        return (ph - pa) / 0.10 * 0.25

    results = []
    for r in clean_ah:
        fh = fair_handicap(r["op_1x2_h"], r["op_1x2_d"], r["op_1x2_a"])
        if fh is None:
            continue
        actual = r["op_ah_line"]
        deviation = actual - fh
        # 偏差 > +0.25: 主队让多了 -> 防冷(客队+受让); < -0.25: 客队让多了 -> 主队有利
        if deviation > 0.25:
            bet = "away"
        elif deviation < -0.25:
            bet = "home"
        else:
            continue
        # 简化判定 AH 输赢: 实际净胜球 vs 让球线
        margin = r["score_home"] - r["score_away"]
        if bet == "home":
            win = margin > actual or (margin == actual and False)  # 走水不算赢
        else:
            win = -margin > actual
        results.append((bet, win))

    if not results:
        return {"n": len(clean_ah), "note": "无偏差>0.25样本"}
    wins = sum(1 for _, w in results if w)
    return {
        "n": len(results),
        "win": wins,
        "win_rate": round(wins / len(results), 4),
        "note": "AH 样本污染严重，结论不可信",
    }


def main() -> None:
    rows = load_clean_outcomes()
    print(f"加载干净 OU 样本: {len(rows)} 场")

    # 全池基线
    total_dist = Counter(total_goals(r) for r in rows)
    over_profits = []
    under_profits = []
    for r in rows:
        side, profit = over_under_push(r)
        if side == "over":
            over_profits.append(profit)
            under_profits.append(-1.0)
        elif side == "under":
            over_profits.append(-1.0)
            under_profits.append(profit)
        else:
            over_profits.append(0.0)
            under_profits.append(0.0)

    baseline = {
        "n": len(rows),
        "avg_total": round(sum(total_goals(r) for r in rows) / len(rows), 3),
        "avg_line": round(sum(r["op_ou_line"] for r in rows) / len(rows), 3),
        "over_roi": round(sum(over_profits) / len(rows), 4),
        "under_roi": round(sum(under_profits) / len(rows), 4),
        "dist": {k: total_dist[k] for k in sorted(total_dist)},
    }

    report = {
        "generated_at": datetime.now().isoformat(),
        "db": str(DB_PATH),
        "baseline": baseline,
        "strategy_2_3_goals": strategy_2_3_goals(rows),
        "strategy_3_4_goals": strategy_3_4_goals(rows),
        "strategy_water_extreme": strategy_water_extreme(rows),
        "strategy_line_deviation_ah": strategy_line_deviation_ah(rows),
        "verdict": None,
    }

    # 人工判定
    verdicts = []
    s23 = report["strategy_2_3_goals"]
    if s23.get("n", 0) > 0:
        if s23["hit_rate_2_or_3"] <= s23["baseline_all_2_or_3"]:
            verdicts.append("2-3球策略命中率未超过全池基线，无 edge。")
        elif s23["over_bet_roi"] <= 0:
            verdicts.append("2-3球策略命中率略高但按大球投注 ROI 为负，仍无 edge。")
        else:
            verdicts.append("2-3球策略出现正 ROI，需进一步显著性检验。")

    s34 = report["strategy_3_4_goals"]
    s34_main = s34.get("line_ge_2_5", {})
    if s34_main.get("n", 0) > 0:
        if s34_main["hit_rate_3_or_4"] <= s34["baseline_all_3_or_4"]:
            verdicts.append("3-4球策略命中率未超过全池基线，无 edge。")
        elif s34_main.get("over_bet_roi", 0) <= 0:
            verdicts.append("3-4球策略命中率略高但按大球投注 ROI 为负，仍无 edge。")
        else:
            verdicts.append("3-4球策略出现正 ROI，需进一步显著性检验。")

    verdicts.append("定律一(AH初盘偏差): 有效 AH 样本严重不足，不可信。")
    verdicts.append("定律二(必发热度反向): 无必发数据，无法回测。")
    verdicts.append("定律三(终盘水位极端化): 见 strategy_water_extreme 明细。")
    report["verdict"] = verdicts

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\n报告已写入: {REPORT_PATH}")


if __name__ == "__main__":
    main()
