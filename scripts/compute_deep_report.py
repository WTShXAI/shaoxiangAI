"""
compute_deep_report.py
=======================
为 footballAI 单场深度决策报告计算核心决策指标。
注码计算委托 bet_core.safe_stake (单一事实源), 含 MAX_STAKE_FRAC 封顶.
纯标准库实现，可复现。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.bet_core import kelly_fraction, safe_stake, FRAC_KELLY, MAX_STAKE_FRAC  # noqa: E402
from pipeline.deep_report import poisson_hda, market_implied as _dr_market_implied  # noqa: E402


def market_implied(odds, margin_method="proportional"):
    """由 1X2 赔率推导隐含概率 + 抽水.

    委托 pipeline.deep_report.market_implied (价值层 SSoT, proportional 去抽水);
    保持本脚本 (probs, margin) 返回契约, 供 build_match 解包使用.
    """
    probs = _dr_market_implied(odds)
    inv = [1.0 / o for o in odds]
    margin = sum(inv) - 1.0
    return probs, margin

def build_match(name, odds, lam_h, lam_a, oip_top_score, oip_ou_over25,
                consensus, operator_flags, bankroll=10000.0, frac_kelly=FRAC_KELLY):
    mk, margin = market_implied(odds)
    md = poisson_hda(lam_h, lam_a)
    outcomes = ["H", "D", "A"]
    rows = []
    for idx, o in enumerate(odds):
        p_mkt = mk[idx]
        p_mod = md[idx]
        edge = p_mod - p_mkt
        ev = p_mod * o - 1.0
        kelly = kelly_fraction(p_mod, o)
        # 使用 bet_core.safe_stake 确保 MAX_STAKE_FRAC 封顶
        stake, _ = safe_stake(p_mod, o, bankroll, frac_kelly=frac_kelly)
        rows.append({
            "outcome": outcomes[idx],
            "odds": o,
            "market_prob": round(p_mkt, 4),
            "model_prob": round(p_mod, 4),
            "edge": round(edge, 4),
            "edge_pct": round(edge * 100, 2),
            "ev": round(ev, 4),
            "ev_pct": round(ev * 100, 2),
            "kelly_full": round(kelly, 4),
            "kelly_half": round(kelly * frac_kelly, 4),
            "stake_unit": round(stake, 2),
        })
    # 最优下注方向 = edge 最大且为正
    best = max(rows, key=lambda r: r["edge"])
    has_edge = best["edge"] > 0
    # 情景 P&L（基于最优方向）
    if has_edge:
        stake = best["stake_unit"]
        win_pnl = stake * (best["odds"] - 1)
        lose_pnl = -stake
        # 期望 P&L = p*win + (1-p)*lose
        exp_pnl = best["model_prob"] * win_pnl + (1 - best["model_prob"]) * lose_pnl
        scenario = {
            "direction": best["outcome"],
            "stake": round(stake, 2),
            "win_pnl": round(win_pnl, 2),
            "lose_pnl": round(lose_pnl, 2),
            "expected_pnl": round(exp_pnl, 2),
            "expected_roi": round(exp_pnl / stake * 100, 2) if stake > 0 else 0.0,
        }
    else:
        scenario = {"direction": None, "note": "无正 edge，建议 PASS"}

    return {
        "match": name,
        "odds": odds,
        "overround_pct": round(margin * 100, 2),
        "market_implied": [round(x, 4) for x in mk],
        "model_poisson": [round(x, 4) for x in md],
        "oip_top_score": oip_top_score,
        "oip_ou_over25_pct": oip_ou_over25,
        "consensus": consensus,
        "operator_flags": operator_flags,
        "rows": rows,
        "best_direction": best["outcome"] if has_edge else "PASS",
        "best_edge_pct": best["edge_pct"],
        "scenario": scenario,
    }

if __name__ == "__main__":
    # ---- 真实比赛：西班牙 vs 比利时 (2026-07-10, 1/4 决赛) ----
    spain_belgium = build_match(
        name="西班牙 vs 比利时 (2026-07-10 · 世界杯1/4决赛)",
        odds=[1.63, 3.92, 5.37],
        lam_h=1.659, lam_a=0.788,
        oip_top_score="1-0 (14.4%)",
        oip_ou_over25=44.25,
        consensus={"bookmaker_count": 16, "pdraw_pct": 22.99, "std_dev": 0.007,
                   "betfair_volume_yuan": 6202000, "strong_signal": False,
                   "wh_iw_pdraw": 22.99, "wh_iw_agreement_pp": 0.67},
        operator_flags=["R2 防平预警" if False else "立主让胜水0.57极低(护主)",
                        "必发主74% ¥459万(资金压倒)"],
    )

    # ---- 对比：挪威 vs 英格兰 (2026-07-11, 1/4 决赛) ----
    # 市场赔率齐全；OIP 用近似 lambda（客让0.5, 强队）→ 演示用
    norway_england = build_match(
        name="挪威 vs 英格兰 (2026-07-11 · 世界杯1/4决赛)",
        odds=[3.65, 3.55, 1.90],
        lam_h=0.95, lam_a=1.62,
        oip_top_score="0-1 (13.5%)",
        oip_ou_over25=52.0,
        consensus={"bookmaker_count": 14, "pdraw_pct": 26.5, "std_dev": 0.006,
                   "betfair_volume_yuan": 5940000, "strong_signal": False},
        operator_flags=["英格兰让0.5/14家一致", "必发客74.63% ¥594万(压倒性)"],
    )

    out = {"matches": [spain_belgium, norway_england]}
    with open("D:/Architecture/deliverables/_deep_report_computed.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2))
