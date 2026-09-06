"""
cross_book_backtest.py — 跨庄共识回测验证 (哨响AI P1 核心验证)

链路: 多庄赔率抽取 → cross_book 共识锚定 → compute_value_layer(cross_book=...)
      → bet_core 半凯利 → 结算(真实赛果) → 资金曲线

数据源:
  主源: football_data.db :: interwetten_odds (IW 140K 场, 含 open/close + 赛果)
  次源: football_data.db :: matches JOIN match_features (WH 30K 场)

核心验证命题:
  「跨庄 consensus 真 edge」— 使用 IW open + IW close 作为双"庄家"视角,
  共识概率 = median(IW_open_devig, IW_close_devig),
  价差 spread_pp = max(|P_open - P_close| × 100),
  走 compute_value_layer(cross_book={consensus, max_spread_pp}) 价值层。

三种策略对比:
  - single_book    : 单庄 IW close→compute_value_layer(cross_book=None), 基准
  - cross_book     : IW open+close 双源共识 → compute_value_layer(cross_book=...)
  - flat_argmax    : 每条下注 argmax 方向平注 (反面基准: 裸跟盘必被抽水吃掉)

注: 单庄去水无 edge (系统铁律)。IW open→close 漂移是真实市场运动信号,
不是"伪 cross_book"; 但 IW open/close 同源, 仅能测试 cross_book 管线正确性;
真正的跨庄验证需 IW+WH 或 GQ+WH 的独立庄家交叉数据。
"""

import csv
import json
import os
import sqlite3
import sys
import time
from statistics import median
from typing import Dict, Any, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts"))

from pipeline.evaluation.metrics import max_drawdown, sharpe_ratio
from pipeline.predictors.unified_predictor import UnifiedPredictor
from pipeline.compute_value_layer import compute_value_layer, market_implied

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "football_data.db")
GQ_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "events.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")

INIT_BANKROLL = 10000.0
FLAT_STAKE_FRAC = 0.01
_DIR = {"H": 0, "D": 1, "A": 2}


def _devig(oh: float, od: float, oa: float) -> Optional[Tuple[float, float, float]]:
    """朴素去水 → 隐含概率 [ph, pd, pa]"""
    if oh <= 1.01 or od <= 1.01 or oa <= 1.01:
        return None
    ih, id_, ia = 1.0 / oh, 1.0 / od, 1.0 / oa
    s = ih + id_ + ia
    return ih / s, id_ / s, ia / s


def load_iw_rows(db_path: str = DB_PATH) -> List[Dict]:
    """从 interwetten_odds 加载 IW 赛事 (含 open/close 赔率 + 赛果)"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT match_date, home_team, away_team, league_name,
               final_result, home_score, away_score,
               open_home_odds, open_draw_odds, open_away_odds,
               close_home_odds, close_draw_odds, close_away_odds,
               home_team_norm, away_team_norm
        FROM interwetten_odds
        WHERE final_result IN ('H', 'D', 'A')
          AND close_home_odds > 1.01 AND close_draw_odds > 1.01 AND close_away_odds > 1.01
          AND open_home_odds > 1.01 AND open_draw_odds > 1.01 AND open_away_odds > 1.01
        ORDER BY match_date ASC
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "date": r[0],
            "home": r[1],
            "away": r[2],
            "league": r[3],
            "result": r[4],
            "score_h": r[5],
            "score_a": r[6],
            "open_h": r[7], "open_d": r[8], "open_a": r[9],
            "close_h": r[10], "close_d": r[11], "close_a": r[12],
            "home_norm": r[13], "away_norm": r[14],
        })
    return out


def load_wh_rows(db_path: str = DB_PATH) -> List[Dict]:
    """从 matches JOIN match_features 加载 WH 赛事 (30K 场, WH close+open)"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT m.match_id, m.match_date, m.final_result,
               m.home_team_name, m.away_team_name, m.league_name,
               f.odds_close_h, f.odds_close_d, f.odds_close_a,
               f.odds_open_h, f.odds_open_d, f.odds_open_a
        FROM matches m
        JOIN match_features f ON m.match_id = f.match_id
        WHERE m.final_result IN ('H', 'D', 'A')
          AND f.odds_close_h > 1.01 AND f.odds_close_d > 1.01 AND f.odds_close_a > 1.01
        ORDER BY m.match_date ASC
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append({
            "mid": r[0], "date": r[1], "result": r[2],
            "home": r[3], "away": r[4], "league": r[5],
            "close_h": r[6], "close_d": r[7], "close_a": r[8],
            "open_h": r[9] if r[9] and r[9] > 1.01 else r[6],
            "open_d": r[10] if r[10] and r[10] > 1.01 else r[7],
            "open_a": r[11] if r[11] and r[11] > 1.01 else r[8],
        })
    return out


def build_cross_book_consensus(close_odds: Tuple[float, float, float],
                                open_odds: Tuple[float, float, float]) -> Optional[Dict]:
    """从两组赔率 (如 IW close + IW open) 构建跨庄共识结构。

    共识概率 = 两组去水概率的中位数 (抗离群)
    max_spread_pp = 两组之间最大概率差 (百分点)
    """
    p_close = _devig(*close_odds)
    p_open = _devig(*open_odds)
    if p_close is None or p_open is None:
        return None

    cons_h = median([p_close[0], p_open[0]])
    cons_d = median([p_close[1], p_open[1]])
    cons_a = median([p_close[2], p_open[2]])

    spreads = [
        abs(p_close[0] - p_open[0]) * 100,
        abs(p_close[1] - p_open[1]) * 100,
        abs(p_close[2] - p_open[2]) * 100,
    ]
    max_spread = max(spreads)

    # 构建最佳价: 两组中每个方向取最高赔率
    best_h = max(close_odds[0], open_odds[0])
    best_d = max(close_odds[1], open_odds[1])
    best_a = max(close_odds[2], open_odds[2])

    return {
        "consensus": {"H": cons_h, "D": cons_d, "A": cons_a},
        "max_spread_pp": max_spread,
        "best": {
            "H": {"odds": round(best_h, 3), "bookmaker": "max(open,close)"},
            "D": {"odds": round(best_d, 3), "bookmaker": "max(open,close)"},
            "A": {"odds": round(best_a, 3), "bookmaker": "max(open,close)"},
        },
        "n_books": 2,
    }


def run_single_book(rows: List[Dict], source: str = "IW") -> Dict:
    """单源模式: IW close only → compute_value_layer (基准)"""
    U = UnifiedPredictor()
    equity = INIT_BANKROLL
    curve = [equity]
    rets = []
    bets = 0
    wins = 0
    skipped = 0
    peak = equity

    for r in rows:
        close_odds = [r["close_h"], r["close_d"], r["close_a"]]
        open_odds = [r["open_h"], r["open_d"], r["open_a"]]
        result = r["result"]

        mid = r.get("mid", f"{r['home']}_{r['away']}_{r['date']}")

        pred = U.predict(
            home=str(mid), away="_",
            odds_h=close_odds[0], odds_d=close_odds[1], odds_a=close_odds[2],
            open_h=open_odds[0], open_d=open_odds[1], open_a=open_odds[2],
        )
        probs = [pred["probabilities"]["H"], pred["probabilities"]["D"], pred["probabilities"]["A"]]

        vl = compute_value_layer(close_odds, probs, bankroll=equity, gate=True, use_flb=True,
                                 cross_book=None)

        if vl["decision"] == "BET":
            di = _DIR[vl["best_direction"]]
            o_actual = close_odds[di]
            stake = vl["scenario"]["stake"]
            if result == vl["best_direction"]:
                equity += stake * (o_actual - 1)
                win = True
            else:
                equity -= stake
                win = False
            bets += 1
            wins += 1 if win else 0
            rets.append(stake * (o_actual - 1) if win else -stake)
        else:
            skipped += 1

        curve.append(equity)
        peak = max(peak, equity)

    roi = equity / INIT_BANKROLL - 1.0
    mdd = max_drawdown(curve)
    hit = wins / bets if bets else 0.0
    sharpe = sharpe_ratio(rets)

    return {
        "mode": "single_book",
        "source": source,
        "n_rows": len(rows),
        "n_bets": bets,
        "n_skipped": skipped,
        "final_equity": round(equity, 2),
        "roi": round(roi, 4),
        "max_drawdown": round(mdd, 4),
        "hit_rate": round(hit, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "_curve": curve,
    }


def run_cross_book(rows: List[Dict], source: str = "IW",
                   min_spread_pp: float = 3.0) -> Dict:
    """跨庄模式: IW close + IW open 双源共识 → compute_value_layer(cross_book=...)

    min_spread_pp: 跨庄价差门槛 (默认 3pp, 低于此值不启用跨庄, 退回单庄 FLB)
    """
    U = UnifiedPredictor()
    equity = INIT_BANKROLL
    curve = [equity]
    rets = []
    bets = 0
    wins = 0
    skipped = 0
    cb_used_count = 0
    peak = equity

    for r in rows:
        close_odds = [r["close_h"], r["close_d"], r["close_a"]]
        open_odds = [r["open_h"], r["open_d"], r["open_a"]]
        result = r["result"]

        mid = r.get("mid", f"{r['home']}_{r['away']}_{r['date']}")

        pred = U.predict(
            home=str(mid), away="_",
            odds_h=close_odds[0], odds_d=close_odds[1], odds_a=close_odds[2],
            open_h=open_odds[0], open_d=open_odds[1], open_a=open_odds[2],
        )
        probs = [pred["probabilities"]["H"], pred["probabilities"]["D"], pred["probabilities"]["A"]]

        # 构建跨庄共识
        cb = build_cross_book_consensus(
            (close_odds[0], close_odds[1], close_odds[2]),
            (open_odds[0], open_odds[1], open_odds[2]),
        )

        if cb and cb["max_spread_pp"] >= min_spread_pp:
            vl = compute_value_layer(close_odds, probs, bankroll=equity,
                                     gate=True, use_flb=True,
                                     cross_book=cb, min_spread_pp=min_spread_pp)
            if vl.get("cross_book_used"):
                cb_used_count += 1
        else:
            # 价差不足 → 退回单庄 FLB
            vl = compute_value_layer(close_odds, probs, bankroll=equity,
                                     gate=True, use_flb=True, cross_book=None)

        if vl["decision"] == "BET":
            di = _DIR[vl["best_direction"]]
            o_actual = close_odds[di]
            stake = vl["scenario"]["stake"]
            if result == vl["best_direction"]:
                equity += stake * (o_actual - 1)
                win = True
            else:
                equity -= stake
                win = False
            bets += 1
            wins += 1 if win else 0
            rets.append(stake * (o_actual - 1) if win else -stake)
        else:
            skipped += 1

        curve.append(equity)
        peak = max(peak, equity)

    roi = equity / INIT_BANKROLL - 1.0
    mdd = max_drawdown(curve)
    hit = wins / bets if bets else 0.0
    sharpe = sharpe_ratio(rets)

    return {
        "mode": "cross_book",
        "source": source,
        "min_spread_pp": min_spread_pp,
        "n_rows": len(rows),
        "n_bets": bets,
        "n_skipped": skipped,
        "n_cross_book_activated": cb_used_count,
        "final_equity": round(equity, 2),
        "roi": round(roi, 4),
        "max_drawdown": round(mdd, 4),
        "hit_rate": round(hit, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "_curve": curve,
    }


def run_flat_argmax(rows: List[Dict]) -> Dict:
    """平注 argmax 反面基准"""
    U = UnifiedPredictor()
    equity = INIT_BANKROLL
    curve = [equity]
    rets = []
    bets = 0
    wins = 0

    for r in rows:
        close_odds = [r["close_h"], r["close_d"], r["close_a"]]
        open_odds = [r["open_h"], r["open_d"], r["open_a"]]
        result = r["result"]

        mid = r.get("mid", f"{r['home']}_{r['away']}_{r['date']}")

        pred = U.predict(
            home=str(mid), away="_",
            odds_h=close_odds[0], odds_d=close_odds[1], odds_a=close_odds[2],
            open_h=open_odds[0], open_d=open_odds[1], open_a=open_odds[2],
        )
        probs = [pred["probabilities"]["H"], pred["probabilities"]["D"], pred["probabilities"]["A"]]

        i = int(max(range(3), key=lambda j: probs[j]))
        o = close_odds[i]
        d = ("H", "D", "A")[i]
        stake = equity * FLAT_STAKE_FRAC

        if result == d:
            equity += stake * (o - 1)
            win = True
        else:
            equity -= stake
            win = False
        bets += 1
        wins += 1 if win else 0
        rets.append(stake * (o - 1) if win else -stake)
        curve.append(equity)

    roi = equity / INIT_BANKROLL - 1.0
    mdd = max_drawdown(curve)
    hit = wins / bets if bets else 0.0
    sharpe = sharpe_ratio(rets)

    return {
        "mode": "flat_argmax",
        "n_rows": len(rows),
        "n_bets": bets,
        "final_equity": round(equity, 2),
        "roi": round(roi, 4),
        "max_drawdown": round(mdd, 4),
        "hit_rate": round(hit, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "_curve": curve,
    }


def _yearly_breakdown(rows: List[Dict], curve: List[float]) -> Dict:
    """按年份统计"""
    yearly = {}
    for i, r in enumerate(rows):
        yr = str(r["date"])[:4]
        if yr not in yearly:
            yearly[yr] = {"n": 0, "eq_start": curve[i], "eq_end": curve[i + 1]}
        yearly[yr]["n"] += 1
        yearly[yr]["eq_end"] = curve[i + 1]

    out = {}
    for yr, v in sorted(yearly.items()):
        roi_yr = v["eq_end"] / v["eq_start"] - 1.0 if v["eq_start"] else 0.0
        out[yr] = {"n_matches": v["n"], "roi": round(roi_yr, 4)}
    return out


def main():
    t0 = time.time()

    # ── 数据加载 ──
    print("=" * 70)
    print("跨庄共识回测 — 哨响AI P1 核心验证")
    print("=" * 70)

    print("\n[1/4] 加载 interwetten_odds (IW 140K 场)...")
    iw_rows = load_iw_rows()
    print(f"  加载 {len(iw_rows)} 场 (IW open+close 赔率 + 赛果)")

    # 年份分布
    years = sorted(set(str(r["date"])[:4] for r in iw_rows))
    print(f"  年份范围: {years[0]}–{years[-1]}")

    print("\n[2/4] 加载 WH 数据 (30K 场)...")
    wh_rows = load_wh_rows()
    print(f"  加载 {len(wh_rows)} 场 (WH close+open 赔率 + 赛果)")

    # ── 回测: IW 数据集 ──
    report = {
        "meta": {
            "title": "跨庄共识回测验证",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "init_bankroll": INIT_BANKROLL,
            "model": "UnifiedPredictor " + UnifiedPredictor.version,
            "data_sources": {
                "iw": f"interwetten_odds: {len(iw_rows)} matches",
                "wh": f"matches+match_features: {len(wh_rows)} matches",
            },
            "note": (
                "IW open+close 作为双'庄家'视角测试 cross_book 管线。"
                "open→close 漂移是真实市场运动信号, 但同庄限制使其不构成 "
                "独立跨庄验证。真正跨庄需 IW+WH 或 GQ+WH 独立庄家交叉数据。"
            ),
        },
        "iw_backtest": {},
        "wh_backtest": {},
    }

    # ── IW 回测 ──
    print("\n[3/4] 运行 IW 回测...")

    print("  [flat_argmax] 反面基准...")
    res_flat_iw = run_flat_argmax(iw_rows)
    curve = res_flat_iw.pop("_curve")
    print(f"    bets={res_flat_iw['n_bets']} roi={res_flat_iw['roi']*100:+.2f}% "
          f"maxDD={res_flat_iw['max_drawdown']*100:.2f}% hit={res_flat_iw['hit_rate']*100:.1f}% "
          f"sharpe={res_flat_iw['sharpe']}")
    report["iw_backtest"]["flat_argmax"] = res_flat_iw
    _write_curve(curve, os.path.join(OUT_DIR, "cross_book_iw_flat_argmax.csv"))

    print("  [single_book] 单庄 IW close → FLB gate...")
    res_single_iw = run_single_book(iw_rows, source="IW")
    curve = res_single_iw.pop("_curve")
    print(f"    bets={res_single_iw['n_bets']} roi={res_single_iw['roi']*100:+.2f}% "
          f"maxDD={res_single_iw['max_drawdown']*100:.2f}% hit={res_single_iw['hit_rate']*100:.1f}% "
          f"sharpe={res_single_iw['sharpe']} skipped={res_single_iw['n_skipped']}")
    report["iw_backtest"]["single_book"] = res_single_iw
    _write_curve(curve, os.path.join(OUT_DIR, "cross_book_iw_single_book.csv"))

    print("  [cross_book] 跨庄 IW open+close 共识 → gate (min_spread=3pp)...")
    res_cb_iw = run_cross_book(iw_rows, source="IW", min_spread_pp=3.0)
    curve = res_cb_iw.pop("_curve")
    print(f"    bets={res_cb_iw['n_bets']} roi={res_cb_iw['roi']*100:+.2f}% "
          f"maxDD={res_cb_iw['max_drawdown']*100:.2f}% hit={res_cb_iw['hit_rate']*100:.1f}% "
          f"sharpe={res_cb_iw['sharpe']} skipped={res_cb_iw['n_skipped']} "
          f"cb_activated={res_cb_iw['n_cross_book_activated']}")
    report["iw_backtest"]["cross_book"] = res_cb_iw
    _write_curve(curve, os.path.join(OUT_DIR, "cross_book_iw_cross_book.csv"))

    # ── WH 回测 (对照) ──
    print("\n[4/4] 运行 WH 对照回测...")

    print("  [flat_argmax] WH 反面基准...")
    res_flat_wh = run_flat_argmax(wh_rows)
    curve = res_flat_wh.pop("_curve")
    print(f"    bets={res_flat_wh['n_bets']} roi={res_flat_wh['roi']*100:+.2f}% "
          f"maxDD={res_flat_wh['max_drawdown']*100:.2f}% hit={res_flat_wh['hit_rate']*100:.1f}%")
    report["wh_backtest"]["flat_argmax"] = res_flat_wh
    _write_curve(curve, os.path.join(OUT_DIR, "cross_book_wh_flat_argmax.csv"))

    print("  [single_book] WH close → FLB gate...")
    res_single_wh = run_single_book(wh_rows, source="WH")
    curve = res_single_wh.pop("_curve")
    print(f"    bets={res_single_wh['n_bets']} roi={res_single_wh['roi']*100:+.2f}% "
          f"maxDD={res_single_wh['max_drawdown']*100:.2f}% hit={res_single_wh['hit_rate']*100:.1f}% "
          f"sharpe={res_single_wh['sharpe']}")
    report["wh_backtest"]["single_book"] = res_single_wh
    _write_curve(curve, os.path.join(OUT_DIR, "cross_book_wh_single_book.csv"))

    print("  [cross_book] WH open+close 共识 → gate (min_spread=3pp)...")
    res_cb_wh = run_cross_book(wh_rows, source="WH", min_spread_pp=3.0)
    curve = res_cb_wh.pop("_curve")
    print(f"    bets={res_cb_wh['n_bets']} roi={res_cb_wh['roi']*100:+.2f}% "
          f"maxDD={res_cb_wh['max_drawdown']*100:.2f}% hit={res_cb_wh['hit_rate']*100:.1f}% "
          f"sharpe={res_cb_wh['sharpe']} cb_activated={res_cb_wh['n_cross_book_activated']}")
    report["wh_backtest"]["cross_book"] = res_cb_wh
    _write_curve(curve, os.path.join(OUT_DIR, "cross_book_wh_cross_book.csv"))

    # ── 汇总对比 ──
    _print_comparison(report)
    elapsed = time.time() - t0
    report["meta"]["elapsed_sec"] = round(elapsed, 1)

    out_json = os.path.join(OUT_DIR, "cross_book_backtest_report.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[done] 报告 → {out_json}  (耗时 {elapsed:.0f}s)")

    return report


def _print_comparison(report: Dict):
    """打印跨庄 vs 单庄对比表"""
    print("\n" + "=" * 70)
    print("跨庄共识 vs 单庄基准 — 对比摘要")
    print("=" * 70)

    for ds_name, ds_key in [("IW (Interwetten 140K)", "iw_backtest"), ("WH (William Hill 30K)", "wh_backtest")]:
        print(f"\n── {ds_name} ──")
        ds = report[ds_key]
        modes = ["flat_argmax", "single_book", "cross_book"]
        header = f"{'Mode':<16} {'Bets':>7} {'ROI':>9} {'MaxDD':>8} {'Hit%':>8} {'Sharpe':>8} {'Skipped':>8}"
        print(header)
        print("-" * len(header))
        for mode in modes:
            if mode not in ds:
                continue
            m = ds[mode]
            cb_info = ""
            if mode == "cross_book":
                cb_info = f"  cb_act={m.get('n_cross_book_activated', '?')}"
            print(f"  {mode:<14} {m['n_bets']:>7} {m['roi']*100:>8.2f}% "
                  f"{m['max_drawdown']*100:>7.2f}% {m['hit_rate']*100:>7.1f}% "
                  f"{str(m['sharpe']):>8} {m.get('n_skipped', 0):>8}{cb_info}")


def _write_curve(curve: List[float], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["step", "equity"])
        for i, eq in enumerate(curve):
            w.writerow([i, round(eq, 2)])


if __name__ == "__main__":
    main()
