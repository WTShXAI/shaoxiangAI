"""
opening_line.py — 从 odds_snapshots 重建「真·初盘主线」(SSoT)

事故 (2026-08-05):
  match_outcomes.op_ou_line / op_ah_line 名字叫 opening, 实际语义是错的。
  gq/db.py:
      ou_pairs = sorted(_fulltime_keys(opening["OU"]), key=lambda t: t[0])
      ou_first = opening["OU"].get(ou_pairs[0][1])       # <-- 取 line 最小的那条
      ah_pairs = sorted(_fulltime_keys(opening["AH"]), key=lambda t: abs(t[0]))

  庄家同一时刻挂的是一整条 OU 梯队, 例:
      OU_1.75  over 1.44 / under 2.56   (抽水 8.5%)
      OU_2.00  over 1.58 / under 2.28
      OU_2.25  over 1.87 / under 1.93   <- 主盘, 两边平衡, 抽水 5.3%
      OU_2.50  over 2.12 / under 1.69
      OU_2.75  over 2.38 / under 1.52
  取「最小线」= 永远取到梯队最底端那条深盘, 大球天然是大热,
  且非主盘线抽水更高。后果:
      op_ou_line 众数 = 0.5 / 1.5 (真实全场 OU 主盘众数应为 2.5)
      庄家隐含大球均值 62%, 实际大球率 74.7% -> 假 edge「无脑买大 +15% ROI」

正确定义:
  主盘(main line) = 同一时刻梯队中 两边去水概率最接近 50/50 的那条线。
                    这是行业标准定义, 也是抽水最低、流动性最好的线。
  初盘(opening)   = 该场最早一批快照 (captured_at 最小) 里的主盘。

用法:
    from pipeline.opening_line import build_opening_lines
    op = build_opening_lines()          # DataFrame: match_key + 主盘 OU/AH/1X2
    op = build_opening_lines(market="OU")

CLI:
    python pipeline/opening_line.py       # 重建 + 校准体检(盲投 ROI 应 ≈ -抽水)
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

# 同一"开盘时刻"的容差: 快照 captured_at 落在最早时间 + 该窗口内视为同一批
OPEN_WINDOW_SEC = 120.0


# 各市场的两个对立边 (side_a, side_b) 与输出列名
_MARKET_SIDES = {
    "OU": ("over", "under"),
    "AH": ("home", "away"),
    # 1X2: 三选 (home/draw/away), 不存在 "两边平衡" 的主盘概念, 直接取最早批次即可
}


def _pick_main_line_1x2(g: pd.DataFrame) -> dict | None:
    """1X2 三选一盘没有「主盘」概念 —— 取开盘批次里最早一条完整 1X2 报价。"""
    sides = ("home", "draw", "away")
    first_ts = g["captured_at"].min()
    batch = g[g["captured_at"] <= first_ts + OPEN_WINDOW_SEC]
    out = {"opened_at": float(first_ts)}
    for s in sides:
        sel = batch.loc[batch["selection"] == s, "odds"]
        if not len(sel):
            return None
        out[s] = float(sel.iloc[0])
    h, d, a = out["home"], out["draw"], out["away"]
    if h <= 1.01 or d <= 1.01 or a <= 1.01:
        return None
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    out["overround"] = round(inv - 1.0, 4)
    out["p_home_devig"] = round((1.0 / h) / inv, 4)
    out["p_draw_devig"] = round((1.0 / d) / inv, 4)
    out["p_away_devig"] = round((1.0 / a) / inv, 4)
    return out


def _pick_main_line(g: pd.DataFrame, market: str = "OU") -> dict | None:
    """从同一批次的梯队里挑主盘: 去水后两边概率最接近 50/50 的线。

    AH 同理 —— 主让球线就是让完之后主客胜率最接近对半的那条,
    梯队两端(深盘/浅盘)抽水更高且方向性强, 不能当"初盘"。
    """
    side_a, side_b = _MARKET_SIDES[market]
    best, best_gap = None, 9e9
    for line, gl in g.groupby("line"):
        o = gl.loc[gl["selection"] == side_a, "odds"]
        u = gl.loc[gl["selection"] == side_b, "odds"]
        if not len(o) or not len(u):
            continue
        ov, un = float(o.iloc[0]), float(u.iloc[0])
        if ov <= 1.01 or un <= 1.01:
            continue
        inv = 1.0 / ov + 1.0 / un
        p_over = (1.0 / ov) / inv
        gap = abs(p_over - 0.5)
        if gap < best_gap:
            best_gap = gap
            best = {"line": float(line), side_a: ov, side_b: un,
                    "overround": round(inv - 1.0, 4),
                    "p_a_devig": round(p_over, 4),
                    "n_lines_offered": int(g["line"].nunique())}
    if best is not None and market == "OU":
        best["p_over_devig"] = best["p_a_devig"]
    return best


def build_opening_lines(db_path: str | None = None,
                        market: str = "OU",
                        full_time_only: bool = True) -> pd.DataFrame:
    """重建每场比赛的初盘主线。

    Args:
        market: "OU" / "AH" / "1X2"
        full_time_only: OU/AH 时是否排除半场盘(1X2 无半场, 参数无效)
    """
    if market not in _MARKET_SIDES and market != "1X2":
        raise NotImplementedError(f"不支持的市场: {market}")

    conn = sqlite3.connect(db_path or GQ_DB)
    try:
        if market == "1X2":
            sql = ("SELECT match_key, captured_at, market, selection, odds "
                   "FROM odds_snapshots WHERE market = '1X2' "
                   "  AND odds IS NOT NULL")
        else:
            pat = f"{market}\\_%" if full_time_only else f"{market}%"
            sql = ("SELECT match_key, captured_at, market, selection, odds, line "
                   "FROM odds_snapshots "
                   f"WHERE market LIKE '{pat}' ESCAPE '\\' "
                   "  AND odds IS NOT NULL AND line IS NOT NULL")
        df = pd.read_sql_query(sql, conn)
    finally:
        conn.close()

    if market != "1X2" and full_time_only:
        # OU_1H_2.50 这类半场盘必须排除
        df = df[~df["market"].str.contains("_1H_|_2H_", na=False)]

    df["odds"] = pd.to_numeric(df["odds"], errors="coerce")
    df["captured_at"] = pd.to_numeric(df["captured_at"], errors="coerce")
    df = df.dropna(subset=["odds", "captured_at", "match_key"])

    rows = []
    for mk, g in df.groupby("match_key"):
        if market == "1X2":
            main = _pick_main_line_1x2(g)
        else:
            t0 = g["captured_at"].min()
            batch = g[g["captured_at"] <= t0 + OPEN_WINDOW_SEC]
            main = _pick_main_line(batch, market=market)
            if main is not None:
                main["match_key"] = mk
                main["opened_at"] = float(t0)
        if main is None:
            continue
        main["match_key"] = mk
        rows.append(main)

    out = pd.DataFrame(rows)
    log.info(f"[opening_line] 重建 {len(out)} 场初盘主线 (市场={market})")
    return out


# ------------------------------------------------------------------ 校准体检
def calibration_check(verbose: bool = True) -> dict:
    """用干净赛果检验重建的主线: 盲投 over / under 的 ROI 应各自 ≈ -抽水。

    这是判断「盘口提取是否正确」的黄金标准 —— 任何一边显著为正,
    要么提取错了, 要么赛果数据有污染。
    """
    from pipeline.clean_outcomes import load_clean_outcomes

    op = build_opening_lines()
    oc = load_clean_outcomes()
    oc["match_key"] = oc["home"].astype(str) + " vs " + oc["away"].astype(str)
    oc["tot"] = oc["score_home"] + oc["score_away"]

    df = op.merge(oc[["match_key", "tot", "league"]], on="match_key", how="inner")
    df = df.drop_duplicates(subset=["match_key"])
    if not len(df):
        return {"n": 0}

    line, tot = df["line"].values, df["tot"].values
    p_over = np.where(tot > line, df["over"].values - 1.0,
                      np.where(tot < line, -1.0, 0.0))
    p_under = np.where(tot < line, df["under"].values - 1.0,
                       np.where(tot > line, -1.0, 0.0))

    res = {
        "n": int(len(df)),
        "avg_line": round(float(line.mean()), 3),
        "median_line": float(np.median(line)),
        "avg_overround": round(float(df["overround"].mean()), 4),
        "market_p_over": round(float(df["p_over_devig"].mean()), 4),
        "actual_over_rate": round(float((tot > line).mean()), 4),
        "actual_push_rate": round(float((tot == line).mean()), 4),
        "roi_blind_over": round(float(p_over.mean()), 4),
        "roi_blind_under": round(float(p_under.mean()), 4),
    }
    res["calibration_gap"] = round(res["actual_over_rate"] - res["market_p_over"], 4)

    if verbose:
        print("=" * 70)
        print("初盘主线重建 — 校准体检")
        print("=" * 70)
        print(f"  匹配场次        : {res['n']}")
        print(f"  平均主盘线      : {res['avg_line']}  (中位 {res['median_line']})")
        print(f"  平均抽水        : {res['avg_overround']:.2%}")
        print(f"  庄家隐含大球率  : {res['market_p_over']:.2%}")
        print(f"  实际大球率      : {res['actual_over_rate']:.2%}   走盘 {res['actual_push_rate']:.2%}")
        print(f"  校准偏差        : {res['calibration_gap']:+.2%}   <- 应在 ±3% 内")
        print()
        print(f"  盲投大球 ROI    : {res['roi_blind_over']:+.2%}")
        print(f"  盲投小球 ROI    : {res['roi_blind_under']:+.2%}")
        print(f"  (健康标志: 两边都为负, 且各自 ≈ -抽水 {-res['avg_overround']:.2%})")

        # 一级判定: 有没有「盲投就赚钱」的假 edge。这是数据是否可信的硬门槛。
        no_fake_edge = (res["roi_blind_over"] < 0 and res["roi_blind_under"] < 0)
        # 二级判定: 校准偏差。允许存在真实的市场偏好(大球偏见), 只要不产生正 ROI。
        well_calibrated = abs(res["calibration_gap"]) < 0.02
        res["pass"] = bool(no_fake_edge)
        res["well_calibrated"] = bool(well_calibrated)

        print()
        if not no_fake_edge:
            print("  判定: FAIL — 存在盲投正收益, 盘口提取仍错或赛果被污染")
        elif well_calibrated:
            print("  判定: PASS — 盘口提取正确, 市场校准良好")
        else:
            side = "小球" if res["calibration_gap"] < 0 else "大球"
            print(f"  判定: PASS — 无假 edge。残留 {res['calibration_gap']:+.2%} 偏向{side}侧,")
            print(f"        这是业界公认的「大球偏见」(散户爱买大球, 庄家在大球侧多收水),")
            print(f"        属真实市场结构而非数据错误。注意: {side}侧仍是负 ROI, 不可直接下注。")
    return res


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ROOT)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    calibration_check()
