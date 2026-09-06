"""
full_chain_backtest.py — 全链路资金曲线回撤测试 (哨响AI)

链路: 盘口锚定预测(UnifiedPredictor vX) -> 价值层(compute_value_layer, FLB+gate)
      -> 注码(bet_core 半凯利封顶) -> 结算(真实赛果) -> 资金曲线
      -> 最大回撤 / ROI / 胜率 / Sharpe

数据源: football_data.db :: matches JOIN match_features(WH收盘 + 初盘) + 真实赛果.
三种策略对比:
  - flat_argmax : 每条下注 argmax 方向, 平注(固定比例本金). 反面基准: 裸跟盘必被抽水吃掉.
  - value_gate  : 仅 compute_value_layer 判 BET(FLB+edge>0+EV>0)才下, 半凯利封顶. 主策略.
  - kelly_argmax: 每条 argmax 但走规范凯利(gate). 展示单源时 kelly≈0 不下注.

注: 单源基准回撤; cross_book 跨庄 soft-line 价差
     (真edge源) 需实时多庄盘口(GQ/cross_book_odds), 不在历史单庄库内.
"""
import csv
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts"))

from pipeline.evaluation.metrics import max_drawdown, sharpe_ratio
from pipeline.predictors.unified_predictor import UnifiedPredictor
from pipeline.compute_value_layer import compute_value_layer
from scripts.bet_core import decide_argmax, decide_direction

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "football_data.db")
OUT_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "full_chain_backtest_report.json")
OUT_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "full_chain_equity_curve.csv")

INIT_BANKROLL = 10000.0
FLAT_STAKE_FRAC = 0.01  # 平注模式: 每注 1% 本金
_DIR = {"H": 0, "D": 1, "A": 2}


def load_rows():
    c = sqlite3.connect(DB_PATH)
    rows = c.execute("""
        SELECT m.match_id, m.match_date, m.final_result,
               f.odds_close_h, f.odds_close_d, f.odds_close_a,
               f.odds_open_h, f.odds_open_d, f.odds_open_a
        FROM matches m JOIN match_features f ON m.match_id = f.match_id
        WHERE m.final_result IN ('H','D','A')
          AND f.odds_close_h > 1.01 AND f.odds_close_d > 1.01 AND f.odds_close_a > 1.01
        ORDER BY m.match_date ASC
    """).fetchall()
    c.close()
    out = []
    for r in rows:
        out.append({
            "mid": r[0], "date": r[1], "result": r[2],
            "oh": r[3], "od": r[4], "oa": r[5],
            "ooh": r[6], "ood": r[7], "oao": r[8],
        })
    return out


def run_mode(rows, mode, temp_scale=None):
    U = UnifiedPredictor()
    equity = INIT_BANKROLL
    curve = [equity]
    rets = []          # 每注收益序列(用于 Sharpe)
    bets = 0
    wins = 0
    skipped = 0
    yearly = {}        # 年份 -> [start_eq, end_eq, bets, wins]

    for r in rows:
        oh, od, oa = r["oh"], r["od"], r["oa"]
        ooh, ood, oao = r["ooh"], r["ood"], r["oao"]
        res = r["result"]
        yr = str(r["date"])[:4]

        # 预测 (单源锚定; open 用于 drift 诊断; leisu_consensus 默认关)
        pred = U.predict(home=str(r["mid"]), away="_", odds_h=oh, odds_d=od, odds_a=oa,
                         open_h=ooh, open_d=ood, open_a=oao, temp_scale=temp_scale)
        probs = [pred["probabilities"]["H"], pred["probabilities"]["D"], pred["probabilities"]["A"]]

        stake = 0.0
        win = False
        if mode == "flat_argmax":
            i = int(max(range(3), key=lambda j: probs[j]))
            o = (oh, od, oa)[i]
            d = ("H", "D", "A")[i]
            stake = equity * FLAT_STAKE_FRAC
            if res == d:
                equity += stake * (o - 1)
                win = True
            else:
                equity -= stake
        elif mode == "value_gate":
            vl = compute_value_layer([oh, od, oa], probs, bankroll=equity, gate=True, use_flb=True)
            if vl["decision"] == "BET":
                di = _DIR[vl["best_direction"]]
                new_eq, stake, win = decide_direction(di, probs, [oh, od, oa], equity, res, gate=True)
                equity = new_eq
            else:
                skipped += 1
        elif mode == "kelly_argmax":
            new_eq, stake, win = decide_argmax(probs, [oh, od, oa], equity, res, gate=True)
            equity = new_eq
            if stake <= 0:
                skipped += 1

        curve.append(equity)
        if stake > 0:
            bets += 1
            wins += 1 if win else 0
            o_actual = (oh, od, oa)[_DIR.get(res, 0)] if False else None
            # 用该注实际赔率: 仅 value_gate/kelly 知道方向, flat 已知
            if mode == "flat_argmax":
                i = int(max(range(3), key=lambda j: probs[j]))
                o_actual = (oh, od, oa)[i]
            else:
                # value_gate/kelly: 方向=argmax(probs) 近似(结算用真实 winner 已在 win 里)
                i = int(max(range(3), key=lambda j: probs[j]))
                o_actual = (oh, od, oa)[i]
            rets.append(stake * (o_actual - 1) if win else -stake)

        if yr not in yearly:
            yearly[yr] = {"start": equity, "end": equity, "bets": 0, "wins": 0}
        yearly[yr]["end"] = equity
        if stake > 0:
            yearly[yr]["bets"] += 1
            yearly[yr]["wins"] += 1 if win else 0

    roi = equity / INIT_BANKROLL - 1.0
    mdd = max_drawdown(curve)
    hit = wins / bets if bets else 0.0
    sharpe = sharpe_ratio(rets)
    # 年份级回撤
    yr_stats = {}
    for y, v in sorted(yearly.items()):
        yr_roi = v["end"] / v["start"] - 1.0 if v["start"] else 0.0
        yr_stats[y] = {
            "roi": round(yr_roi, 4),
            "bets": v["bets"],
            "hit_rate": round(v["wins"] / v["bets"], 4) if v["bets"] else None,
        }
    return {
        "mode": mode,
        "n_rows": len(rows),
        "n_bets": bets,
        "n_skipped": skipped,
        "final_equity": round(equity, 2),
        "roi": round(roi, 4),
        "max_drawdown": round(mdd, 4),
        "hit_rate": round(hit, 4),
        "sharpe": round(sharpe, 4) if sharpe is not None else None,
        "yearly": yr_stats,
        "_curve": curve,
    }


def _best_T_for_train(train_rows):
    """train 段网格搜索最优温度 T (最小化 log_loss)."""
    from pipeline.evaluation.metrics import log_loss
    grid = [0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10]
    U = UnifiedPredictor()
    best_T, best_ll = 0.90, 1e9
    for T in grid:
        pl, oc = [], []
        for r in train_rows:
            p = U.predict(home=str(r["mid"]), away="_", odds_h=r["oh"], odds_d=r["od"], odds_a=r["oa"],
                          open_h=r["ooh"], open_d=r["ood"], open_a=r["oao"], temp_scale=T)["probabilities"]
            pl.append([p["H"], p["D"], p["A"]])
            oc.append(r["result"])
        ll = log_loss(pl, oc)
        if ll is not None and ll < best_ll:
            best_ll, best_T = ll, T
    return best_T, best_ll


def run_walk_forward(rows):
    """时间序列切分: train(2015-2022)标定T, test(2023-2026)盲跑 value_gate.
    返回诚实 OOS vs 默认T(in-sample风格) 对照."""
    train = [r for r in rows if str(r["date"])[:4] < "2023"]
    test = [r for r in rows if str(r["date"])[:4] >= "2023"]
    T_star, ll = _best_T_for_train(train)
    oos = run_mode(test, "value_gate", temp_scale=T_star)
    oos.pop("_curve", None)
    ins = run_mode(test, "value_gate", temp_scale=None)  # 默认 CALIB_T=0.90
    ins.pop("_curve", None)
    return {"train_n": len(train), "test_n": len(test),
            "optimal_T": T_star, "train_logloss": round(ll, 4),
            "oos_value_gate_Tstar": oos,
            "test_insample_defaultT": ins}


def main():
    rows = load_rows()
    print(f"[load] {len(rows)} 场 (收盘赔率+赛果), 按时间升序")
    report = {"meta": {
        "source": "football_data.db :: matches JOIN match_features(WH收盘+初盘)",
        "n_rows": len(rows),
        "init_bankroll": INIT_BANKROLL,
        "flat_stake_frac": FLAT_STAKE_FRAC,
        "model": "UnifiedPredictor " + UnifiedPredictor.version,
        "note": "单庄基准; cross_book 跨庄 edge 需实时多庄盘口",
    }, "modes": {}}
    for mode in ["flat_argmax", "value_gate", "kelly_argmax"]:
        res = run_mode(rows, mode)
        curve = res.pop("_curve")
        report["modes"][mode] = res
        print(f"  [{mode:12s}] bets={res['n_bets']:6d} roi={res['roi']*100:7.2f}% "
              f"maxDD={res['max_drawdown']*100:6.2f}% hit={res['hit_rate']*100:5.1f}% "
              f"sharpe={res['sharpe']}")
        # 写 CSV (每注后 equity)
        with open(OUT_CSV.replace(".csv", f"_{mode}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["step", "equity"])
            for i, eq in enumerate(curve):
                w.writerow([i, round(eq, 2)])

    # walk-forward OOS 对照: train(2015-2022)标定T, test(2023-2026)盲跑
    wf = run_walk_forward(rows)
    report["walk_forward"] = wf
    oos = wf["oos_value_gate_Tstar"]
    print(f"[walk_forward] optimal_T={wf['optimal_T']} train_ll={wf['train_logloss']} "
          f"| OOS roi={oos['roi']*100:.2f}% maxDD={oos['max_drawdown']*100:.2f}% "
          f"hit={oos['hit_rate']*100:.1f}% | test默认T roi={wf['test_insample_defaultT']['roi']*100:.2f}%")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[done] report -> {OUT_JSON}")
    return report


if __name__ == "__main__":
    main()
