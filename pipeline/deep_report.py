"""
pipeline/deep_report.py
========================
深度决策报告核心模块（L0 价值层）。从足球AI预测结果推导"该不该下、下多少、预期赚多少"。

核心函数：
  - poisson_hda(lam_h, lam_a)          由 OIP 期望进球推导 1X2 概率
  - kelly_fraction(p, odds)           凯利注码比
  - compute_value_layer(...)          价值层主函数：edge / EV / 凯利 / 情景PnL / 决策

设计要点：
  - 模型概率来自 OIP 比分矩阵边缘（bridge_service 内由 predict_score 的 M 推导），
    或独立由 lambda 推导；市场概率来自赔率去抽水。
  - edge = 模型概率 − 市场隐含概率；EV = 模型概率×赔率 − 1；凯利 = (p·odds−1)/(odds−1)。
  - ⚠️ compute_value_layer 的 stake_unit 仅供展示分析，实际下注必须走 scripts/bet_core.py
    的 safe_stake (含 MAX_STAKE_FRAC=10% 封顶 + 分歧闸门守卫)。
  - 所有结论可复现、纯标准库，无外部依赖。
"""
from __future__ import annotations
import math
from typing import Dict, Any, List, Optional, Tuple

# ── 硬约束: 单注上限 10% 本金（与 bet_core.MAX_STAKE_FRAC 保持一致）──
_MAX_STAKE_FRAC = 0.10

# ── E2 P0-5/P0-6 + P2-16: 注码/凯利统一走 bet_core (SSoT, 含10%封顶+分歧闸门+审计) ──
# bet_core 是注码/凯利的唯一事实源; 不再保留本地降级副本, 杜绝公式漂移.
# 若 bet_core 不可用 (缺 PyYAML 等), 直接报错而非静默用过期副本.
try:
    from scripts.bet_core import safe_stake as _bet_core_safe_stake, kelly_fraction
    _HAS_BET_CORE = True
except Exception as _bet_core_err:  # pragma: no cover
    raise ImportError(
        "deep_report 依赖 scripts.bet_core (注码/凯利单一事实源); "
        "请先安装 PyYAML (requirements.txt: PyYAML>=6.0)."
    ) from _bet_core_err


def _capped_stake(p: float, odds: float, bankroll: float,
                  frac_kelly: float = 0.5, gate: bool = True,
                  source: str = "deep_report") -> float:
    """统一封顶注码 (E2 P0-5 修复 kelly>1 全押坑).

    始终走 bet_core.safe_stake (SSoT: 10%封顶 + PROD NO-GO + 分歧闸门 + 审计);
    gate 透传给 safe_stake (gate=False = 裸下注不闸门对照语义, 与 bet_core 一致)。
    """
    stake, _ = _bet_core_safe_stake(
        p, odds, bankroll, frac_kelly=frac_kelly, gate=gate, source=source)
    return float(stake)


def poisson_hda(lam_h: float, lam_a: float, max_goals: int = 12) -> tuple:
    """由双方期望进球 λ 推导 1X2 概率 (P主胜, P平, P客胜)。"""
    def pmf(lam: float, k: int) -> float:
        return math.exp(-lam) * (lam ** k) / math.factorial(k)
    ph = pd = pa = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = pmf(lam_h, i) * pmf(lam_a, j)
            if i > j:
                ph += p
            elif i == j:
                pd += p
            else:
                pa += p
    return ph, pd, pa


def market_implied(odds: List[float]) -> List[float]:
    """由 1X2 赔率推导隐含概率（proportional 法剔除抽水）。"""
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


def deoverround(oh: float, od: float, oa: float) -> Tuple[float, float, float]:
    """1X2 去抽水 → 隐含 P(H),P(D),P(A)。本地纯标准库版(避免依赖 scipy 版 score_model)。"""
    o = 1.0 / oh + 1.0 / od + 1.0 / oa
    return (1.0 / oh) / o, (1.0 / od) / o, (1.0 / oa) / o


def compute_value_layer(
    odds: List[float],
    model_probs: List[float],
    bankroll: float = 10000.0,
    frac_kelly: float = 0.5,
    overround: Optional[float] = None,
    gate: bool = True,
) -> Dict[str, Any]:
    """
    价值层主函数。

    参数：
      odds        : [主胜, 平局, 客胜] 十进制赔率
      model_probs : [P主胜, P平, P客胜] 模型概率（OIP矩阵边缘或lambda推导）
      bankroll    : 本金基准（用于算出建议注码绝对额），默认 10000
      frac_kelly  : 凯利比例（0.5=半凯利），默认 0.5
      overround   : 抽水（可选，仅用于展示；不传则按赔率倒数和算）

    返回：
      {
        odds, market_implied, model_prob, overround_pct,
        rows: [{outcome, odds, market_prob, model_prob, edge, edge_pct, ev, ev_pct, kelly_full, kelly_half, stake_unit}],
        best_direction, best_edge_pct,
        decision: "PASS"|"BET",
        decision_text,
        scenario: {direction, stake, win_pnl, lose_pnl, expected_pnl, expected_roi}
      }
    """
    if overround is None:
        overround = (sum(1.0 / o for o in odds) - 1.0)
    mk = market_implied(odds)
    outcomes = ["H", "D", "A"]
    rows: List[Dict[str, Any]] = []
    for idx, o in enumerate(odds):
        p_mkt = mk[idx]
        p_mod = model_probs[idx]
        edge = p_mod - p_mkt
        ev = p_mod * o - 1.0
        k = kelly_fraction(p_mod, o)
        # stake_unit 仅供展示; 实际下注须走 bet_core.safe_stake (含 10% 封顶)
        stake_raw = bankroll * k * frac_kelly
        stake_capped = min(stake_raw, bankroll * _MAX_STAKE_FRAC)
        rows.append({
            "outcome": outcomes[idx],
            "odds": o,
            "market_prob": round(p_mkt, 4),
            "model_prob": round(p_mod, 4),
            "edge": round(edge, 4),
            "edge_pct": round(edge * 100, 2),
            "ev": round(ev, 4),
            "ev_pct": round(ev * 100, 2),
            "kelly_full": round(k, 4),
            "kelly_half": round(k * frac_kelly, 4),
            "stake_unit": round(stake_capped, 2),
            "stake_uncapped": round(stake_raw, 2),  # 对比用
        })

    best = max(rows, key=lambda r: r["edge"])
    # 只有正期望价值(EV>0 → 凯利>0)才下注; 仅 edge>0 但被抽水吃掉仍 PASS
    positive_ev = best["ev"] > 0 and gate  # E2 P0-6: 分歧闸门未过→强制 PASS

    if positive_ev:
        stake = best["stake_unit"]
        win_pnl = stake * (best["odds"] - 1)
        lose_pnl = -stake
        exp_pnl = best["model_prob"] * win_pnl + (1 - best["model_prob"]) * lose_pnl
        scenario = {
            "direction": best["outcome"],
            "stake": round(stake, 2),
            "win_pnl": round(win_pnl, 2),
            "lose_pnl": round(lose_pnl, 2),
            "expected_pnl": round(exp_pnl, 2),
            "expected_roi": round(exp_pnl / stake * 100, 2) if stake > 0 else 0.0,
        }
        decision = "BET"
        decision_text = (
            f"下注 · {best['outcome']} edge +{best['edge_pct']:.2f}% · "
            f"EV +{best['ev_pct']:.2f}% · 半凯利 ¥{stake:.0f}/万本金"
        )
    else:
        scenario = {"direction": None, "note": "全方向负 EV 或零 edge，建议 PASS"}
        decision = "PASS"
        decision_text = "PASS · 全方向负 EV（抽水吃掉 edge），不接盘"

    return {
        "odds": odds,
        "market_implied": [round(x, 4) for x in mk],
        "model_prob": [round(x, 4) for x in model_probs],
        "overround_pct": round(overround * 100, 2),
        "rows": rows,
        "best_direction": best["outcome"] if positive_ev else "PASS",
        "best_edge_pct": best["edge_pct"],
        "decision": decision,
        "decision_text": decision_text,
        "scenario": scenario,
    }


def compute_submarket_value(
    legs: List[Dict[str, Any]],
    bankroll: float = 10000.0,
    frac_kelly: float = 0.5,
    gate: bool = True,
) -> Dict[str, Any]:
    """
    子市场价值层（与 compute_value_layer 同契约，但对任意子市场腿通用）。

    适用：O/U 大小球、波胆(CS)、双庄平局共识(DC draw)、让球(AH) 等
    “单一结果 × 跨庄最优赔率” 的腿。生产默认不需模型概率——
    据 v6 铁律，子市场“模型概率”最佳估计 = 跨庄共识隐含概率（或 OIP 推导概率），
    真实 edge 仅来自跨庄价差（best_odds 与共识隐含之差），由本函数用 best_odds 算 EV。

    参数 legs: [{label, best_odds, consensus_prob}]
      - best_odds       : 跨庄最优十进制赔率
      - consensus_prob  : 跨庄共识隐含概率 / OIP 推导概率（已去抽水）
    返回：{rows, best_label, best_edge_pct, decision, decision_text, scenario}
    """
    if not legs:
        return {"rows": [], "decision": "PASS", "decision_text": "无子市场腿"}
    rows: List[Dict[str, Any]] = []
    for leg in legs:
        o = float(leg["best_odds"])
        p = float(leg["consensus_prob"])
        mkt = (1.0 / o) if o > 0 else 0.0  # 单庄隐含(含抽水)；跨庄 edge 已由 consensus 去抽水
        edge = p - mkt
        ev = p * o - 1.0
        k = kelly_fraction(p, o)
        rows.append({
            "label": leg["label"],
            "best_odds": round(o, 3),
            "model_prob": round(p, 4),
            "market_prob": round(mkt, 4),
            "edge": round(edge, 4),
            "edge_pct": round(edge * 100, 2),
            "ev": round(ev, 4),
            "ev_pct": round(ev * 100, 2),
            "kelly_full": round(k, 4),
            "kelly_half": round(k * frac_kelly, 4),
            "stake_unit": round(bankroll * k * frac_kelly, 2),
        })
    best = max(rows, key=lambda r: r["edge"])
    positive_ev = best["ev"] > 0 and gate  # E2 P0-6: 分歧闸门未过→强制 PASS
    if positive_ev:
        stake = _capped_stake(best["model_prob"], best["best_odds"], bankroll,
                              frac_kelly=frac_kelly, gate=gate, source="deep_report_submarket_legs")
        win_pnl = stake * (best["best_odds"] - 1)
        lose_pnl = -stake
        exp_pnl = best["model_prob"] * win_pnl + (1 - best["model_prob"]) * lose_pnl
        scenario = {
            "label": best["label"], "stake": round(stake, 2),
            "win_pnl": round(win_pnl, 2), "lose_pnl": round(lose_pnl, 2),
            "expected_pnl": round(exp_pnl, 2),
            "expected_roi": round(exp_pnl / stake * 100, 2) if stake > 0 else 0.0,
        }
        decision = "BET"
        decision_text = (
            f"子市场下注 · {best['label']} edge +{best['edge_pct']:.2f}% · "
            f"EV +{best['ev_pct']:.2f}% · 半凯利 ¥{stake:.0f}/万本金"
        )
    else:
        scenario = {"label": None, "note": "全腿负 EV，PASS"}
        decision = "PASS"
        decision_text = "PASS · 子市场全腿负 EV（抽水吃掉 edge）"
    return {
        "rows": rows,
        "best_label": best["label"] if positive_ev else "PASS",
        "best_edge_pct": best["edge_pct"],
        "decision": decision,
        "decision_text": decision_text,
        "scenario": scenario,
    }


def model_probs_from_matrix(M: Any) -> List[float]:
    """由 OIP 比分概率矩阵 M (numpy 2D) 推导 1X2 边缘概率 [P主, P平, P客]。"""
    import numpy as np
    M = np.asarray(M, dtype=float)
    n = M.shape[0]
    ph = float(sum(M[i, j] for i in range(n) for j in range(n) if i > j))
    pd = float(sum(M[i, j] for i in range(n) for j in range(n) if i == j))
    pa = float(sum(M[i, j] for i in range(n) for j in range(n) if i < j))
    return [ph, pd, pa]


def consensus_probs(books: List[List[float]]) -> List[float]:
    """跨庄共识隐含概率：各庄去抽水隐含概率取均值再归一。

    设计依据（FootballAI v6 铁律）：模型对 1X2 无超越赔率的信息优势，
    故"模型概率"的最佳估计 = 跨庄共识隐含概率；1X2 的真实 edge 仅来自
    跨庄价差（soft line / 套利空间），由 compute_value_layer 用 best_odds 计算。
    books = [[oh, od, oa], ...]，单庄时共识=该庄 → edge≈0 → PASS。"""
    valid = [b for b in books if b and all(x > 0 for x in b)]
    if not valid:
        return [0.0, 0.0, 0.0]
    imp = [market_implied(b) for b in valid]
    n = len(imp)
    avg = [sum(p[i] for p in imp) / n for i in range(3)]
    s = sum(avg) or 1.0
    return [x / s for x in avg]


# ───────────────────────────────────────────────────────────────────────────
# 子市场价值层 (P1) — 大小球 / 平局共识 / 波胆
# 诚实约束 (v6 铁律): OIP 矩阵 λ 同源主盘, 故子市场 edge 只来自
#   (a) 跨市场不一致: 主盘1X2隐含总进球 vs 大小球盘隐含总进球 矛盾
#   (b) 跨庄共识溢价: ≥2家独立庄家(或WH×IW)对平局/波胆的定价 > 主盘同源定价
# 绝不"模型 vs 同源盘"循环论证。
# ───────────────────────────────────────────────────────────────────────────

def _poisson_pmf(lam: float, k: int) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _solve_oip_simple(ph: float, pd_: float, pa: float, maxg: int = 8) -> Tuple[float, float]:
    """纯标准库 OIP λ 求解 (粗网格), 仅 fallback 用; 正常由 bridge_service 传 M。"""
    best, bestr = (1.3, 1.1), 1e9
    for li in range(3, 45):
        lh = li * 0.1
        for lj in range(3, 45):
            la = lj * 0.1
            eh = sum(_poisson_pmf(lh, i) * sum(_poisson_pmf(la, j) for j in range(maxg + 1))
                     for i in range(maxg + 1))
            ed = sum(_poisson_pmf(lh, i) * _poisson_pmf(la, i) for i in range(maxg + 1))
            r = (eh - ph) ** 2 + (ed - pd_) ** 2
            if r < bestr:
                bestr, best = r, (lh, la)
    return best


def _score_matrix_py(lh: float, la: float, maxg: int = 8) -> List[List[float]]:
    col = [_poisson_pmf(lh, i) for i in range(maxg + 1)]
    row = [_poisson_pmf(la, j) for j in range(maxg + 1)]
    return [[col[i] * row[j] for j in range(maxg + 1)] for i in range(maxg + 1)]


def poisson_p_over(M: Any, line: float) -> float:
    """P(总进球 > line) from 比分概率矩阵 M (numpy 或 list-of-lists, 0-indexed)。"""
    n = len(M)
    p = 0.0
    for i in range(n):
        for j in range(n):
            if i + j > line:
                p += M[i][j]
    return float(p)


def ou_value(oh: float, od: float, oa: float, ou_line: float, over_odds: float, under_odds: float,
             model_m: Optional[Any] = None, bankroll: float = 10000.0, frac_kelly: float = 0.5,
             gap_threshold: float = 0.05, gate: bool = True) -> Dict[str, Any]:
    """
    大小球价值层 (跨市场不一致): 主盘1X2隐含期望进球 vs 大小球盘隐含总进球。

    诚实说明: 模型 P(大) 由主盘1X2反推的 Poisson 期望进球得出(同源主盘),
    故不宣称"模型优势", 而是检查"主盘1X2隐含的总进球预期"与"大小球盘隐含的
    总进球预期"是否出现跨市场矛盾(同一赛事两家盘口定价不一致) → 可下注信号。
    仅当 |gap| >= gap_threshold(默认5pp) 且 该侧 EV>0 才下注, 否则 PASS。
    """
    if model_m is None:
        ph, mpd, pa = deoverround(oh, od, oa)
        lh, la = _solve_oip_simple(ph, mpd, pa)
        model_m = _score_matrix_py(lh, la, 8)
    model_p_over = poisson_p_over(model_m, ou_line)
    model_p_under = 1.0 - model_p_over
    inv = 1.0 / over_odds + 1.0 / under_odds
    mkt_p_over = (1.0 / over_odds) / inv
    mkt_p_under = (1.0 / under_odds) / inv
    gap = model_p_over - mkt_p_over            # >0: 主盘暗示比OU盘更多球
    ev_over = model_p_over * (over_odds - 1) - (1 - model_p_over)
    ev_under = model_p_under * (under_odds - 1) - (1 - model_p_under)

    cands = []
    if ev_over > 0 and gap >= gap_threshold:
        cands.append(("over", model_p_over, over_odds, ev_over))
    if ev_under > 0 and (-gap) >= gap_threshold:
        cands.append(("under", model_p_under, under_odds, ev_under))

    if cands:
        side, p, odds, ev = max(cands, key=lambda c: c[3])
        stake = _capped_stake(p, odds, bankroll, frac_kelly=frac_kelly,
                              gate=gate, source="deep_report_submarket")
        if stake <= 0:
            # E2 P0-6: 分歧闸门未过 / kelly<=0 → 不下注
            decision = "PASS"
            decision_text = "PASS · 分歧闸门未过(gate=False) 或负凯利, 不下注"
            scenario: Dict[str, Any] = {"note": "gate closed or non-positive kelly"}
            return {
                "ou_line": ou_line,
                "over_odds": over_odds, "under_odds": under_odds,
                "model_p_over": round(model_p_over, 4), "model_p_under": round(model_p_under, 4),
                "market_p_over": round(mkt_p_over, 4), "market_p_under": round(mkt_p_under, 4),
                "gap_pp": round(gap * 100, 2), "ev_over_pct": round(ev_over * 100, 2),
                "ev_under_pct": round(ev_under * 100, 2),
                "decision": decision, "decision_text": decision_text, "scenario": scenario,
            }
        win = stake * (odds - 1)
        lose = -stake
        exp = p * win + (1 - p) * lose
        decision = "BET"
        decision_text = (f"大小球{'大' if side == 'over' else '小'}{ou_line} · "
                         f"跨市场gap {gap*100:+.1f}pp · EV +{ev*100:.2f}% · 半凯利 ¥{stake:.0f}/万")
        scenario = {"side": side, "stake": round(stake, 2), "win_pnl": round(win, 2),
                    "lose_pnl": round(lose, 2), "expected_pnl": round(exp, 2),
                    "expected_roi": round(exp / stake * 100, 2) if stake > 0 else 0.0}
    else:
        decision = "PASS"
        decision_text = "PASS · 大小球主盘与OU盘一致(或gap<5pp/负EV), 无跨市场不一致edge"
        scenario = {"note": "无跨市场不一致信号"}
    return {
        "ou_line": ou_line,
        "over_odds": over_odds,
        "under_odds": under_odds,
        "model_p_over": round(model_p_over, 4),
        "model_p_under": round(model_p_under, 4),
        "market_p_over": round(mkt_p_over, 4),
        "market_p_under": round(mkt_p_under, 4),
        "gap_pp": round(gap * 100, 2),
        "ev_over_pct": round(ev_over * 100, 2),
        "ev_under_pct": round(ev_under * 100, 2),
        "decision": decision, "decision_text": decision_text, "scenario": scenario,
    }


def draw_consensus_value(primary_oh: float, primary_od: float, primary_oa: float,
                         consensus_pd: float, strong: bool = False,
                         best_draw_odds: Optional[float] = None, bankroll: float = 10000.0,
                         frac_kelly: float = 0.5,
                         gate: bool = True) -> Dict[str, Any]:
    """
    平局共识价值层 (跨庄溢价): ≥2家独立庄家(或WH×IW)共识P(平) vs 主盘隐含P(平)。
    共识P(平)来自独立定价源(非主盘同源) → 可证伪的真实 edge。
    若 共识P(平) > 主盘隐含P(平) 且在(跨庄最优)平局赔率下 EV>0 → BET。
    """
    if consensus_pd is None:
        return {"decision": "PASS", "decision_text": "PASS · 无跨庄平局共识(单庄), 不可证伪",
                "scenario": {"note": "no consensus"}}
    _, mkt_pd, _ = deoverround(primary_oh, primary_od, primary_oa)
    edge = consensus_pd - mkt_pd
    odds = best_draw_odds if best_draw_odds else primary_od
    ev = consensus_pd * (odds - 1) - (1 - consensus_pd)
    k = kelly_fraction(consensus_pd, odds)
    stake = _capped_stake(consensus_pd, odds, bankroll, frac_kelly=frac_kelly,
                          gate=gate, source="deep_report_draw_consensus")
    if ev > 0 and stake > 0:  # E2 P0-6: gate=False→stake=0→落入 PASS 分支
        win = stake * (odds - 1)
        lose = -stake
        exp = consensus_pd * win + (1 - consensus_pd) * lose
        decision = "BET"
        flag = "强信号" if strong else "共识"
        decision_text = (f"平局(共识) · edge +{edge*100:.2f}pp · EV +{ev*100:.2f}% · "
                         f"{flag} · 半凯利 ¥{stake:.0f}/万")
        scenario = {"direction": "D", "stake": round(stake, 2), "win_pnl": round(win, 2),
                    "lose_pnl": round(lose, 2), "expected_pnl": round(exp, 2),
                    "expected_roi": round(exp / stake * 100, 2) if stake > 0 else 0.0}
    else:
        decision = "PASS"
        decision_text = (f"PASS · 平局共识P平{consensus_pd*100:.1f}% "
                         f"≤ 主盘{mkt_pd*100:.1f}%(或负EV)")
        scenario = {"note": "共识未显示平局溢价"}
    return {"consensus_pd": round(consensus_pd, 4), "market_pd": round(mkt_pd, 4),
            "edge_pp": round(edge * 100, 2), "ev_pct": round(ev * 100, 2),
            "best_odds": round(odds, 3) if odds else None,
            "strong": strong, "decision": decision, "decision_text": decision_text,
            "scenario": scenario}


def correct_score_value(model_m: Any, score_odds: Optional[Dict] = None, top_n: int = 3,
                        bankroll: float = 10000.0, frac_kelly: float = 0.5,
                        overconf: Optional[float] = None,
                        cs_ev_threshold: float = 0.0,
                        gate: bool = True) -> Dict[str, Any]:
    """
    波胆 TOP-N 视图 / 价值层 (统一入口, 取代原 correct_score_scan)。

    - score_odds 提供时: score_odds={(i,j): 跨庄最优十进制赔率}。
      真实 edge = 跨庄CS价 vs OIP fair值, 按 ev_pct 降序, 输出 BET/PASS。
    - score_odds 缺失时(当前数据集无任何CS盘): 诚实降级为概率扫描,
      按 prob 降序, decision="SCAN", edge_available=False —— 绝不伪称edge (符 v6 铁律:
      1X2有效市场, 模型无超越赔率信息优势, 子市场edge只来自跨庄价差)。

    过自信修正 (WC校准, 2026-07-11 落实):
    - overconf: 模型概率过自信倍数 (WC校准=1.93, 即模型TOP1均概率17.5% vs 真实命中9.1%)。
      提供时, 用有效概率 p_eff = p / overconf 算EV与凯利 → 把"小edge假价值"压成负EV→PASS,
      避免WC上"EV>0即BET"亏钱(合成6%edge→ROI -26.6%)。仅取top1下注(分散低命中不划算)。
    - cs_ev_threshold: EV百分比阈值(如 15.0 表示需 +15% 才BET)。默认0.0=仅过自信收缩门。
      非WC联赛 overconf=None → 不收缩, 保持原行为(诚实: 无跨联赛过自信数据)。

    返回结构始终为 dict: {rows:[{score,prob,prob_eff,fair_decimal/odds,ev_pct,edge,...}],
                          decision, edge_available, decision_text, scenario}
    """
    n = len(model_m)
    flat = [model_m[i][j] for i in range(n) for j in range(n)]

    if score_odds:
        rows = []
        for (i, j), odds in score_odds.items():
            idx = i * n + j
            if idx >= len(flat):
                continue
            p = flat[idx]
            if p <= 0 or odds <= 1:
                continue
            # 过自信收缩: 用真实命中率反推的有效概率算EV (WC校准 overconf=1.93)
            p_eff = p / overconf if overconf and overconf > 0 else p
            ev = p_eff * odds - 1
            k = kelly_fraction(p_eff, odds)
            rows.append({"score": f"{i}-{j}", "prob": round(p, 4),
                         "prob_eff": round(p_eff, 4), "odds": odds,
                         "ev_pct": round(ev * 100, 2), "kelly_half": round(k * frac_kelly, 4),
                         "stake": round(_capped_stake(p_eff, odds, bankroll,
                                                     frac_kelly=frac_kelly, gate=gate,
                                                     source="deep_report_cs"), 2),
                         "edge": True})
        rows.sort(key=lambda r: r["ev_pct"], reverse=True)
        # 仅过自信收缩后EV仍超过阈值才下注(默认阈值0.0=仅收缩门); 仅取top1
        best = rows[0] if rows and rows[0]["ev_pct"] > cs_ev_threshold else None
        if best and gate:  # E2 P0-6: 分歧闸门未过→强制 PASS
            stake = best["stake"]
            win = stake * (best["odds"] - 1)
            lose = -stake
            exp = best["prob_eff"] * win + (1 - best["prob_eff"]) * lose
            decision = "BET"
            decision_text = (f"波胆 {best['score']} · 有效P{best['prob_eff']*100:.1f}%"
                             f"(模型{best['prob']*100:.1f}%) · EV +{best['ev_pct']:.2f}%"
                             f" · 半凯利 ¥{stake:.0f}/万")
            scenario = {"score": best["score"], "stake": round(stake, 2), "win_pnl": round(win, 2),
                        "lose_pnl": round(lose, 2), "expected_pnl": round(exp, 2),
                        "expected_roi": round(exp / stake * 100, 2) if stake > 0 else 0.0}
        else:
            decision = "PASS"
            decision_text = "PASS · 波胆跨庄无正EV(过自信收缩后)"
            scenario = {"note": "no CS edge after overconf shrinkage"}
        return {"rows": rows[:top_n], "decision": decision, "edge_available": True,
                "decision_text": decision_text, "scenario": scenario}

    # ── 无跨庄CS盘: 诚实概率扫描 (TOP-N by prob, 不宣称edge) ──
    order = sorted(range(len(flat)), key=lambda k: -flat[k])[:top_n]
    rows = []
    for k in order:
        i, j = divmod(k, n)
        p = flat[k]
        p_eff = p / overconf if overconf and overconf > 0 else p
        rows.append({"score": f"{i}-{j}", "prob": round(p, 4),
                     "prob_eff": round(p_eff, 4),
                     "fair_decimal": round(1 / p, 2) if p > 0 else None,
                     "fair_eff_decimal": round(1 / p_eff, 2) if p_eff > 0 else None,
                     "value": None, "edge": False})
    eff_note = " (已按overconf收缩展示有效概率)" if overconf else ""
    return {"rows": rows, "decision": "SCAN", "edge_available": False,
            "decision_text": "SCAN · 无跨庄波胆价, 仅展示fair value(同源主盘), 不宣称edge" + eff_note,
            "scenario": {"note": "no cross-book CS odds in dataset; value layer gated per v6 iron law"}}
