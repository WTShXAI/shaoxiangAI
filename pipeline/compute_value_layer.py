"""
pipeline/compute_value_layer.py
================================
价值层 · 单一事实源 (SSoT)

从预测结果推导"该不该下、下多少、预期赚多少"。
被 bridge_service / quant_engine / quant_demo / 多个 scripts 调用。

铁律：
  - 本模块是价值层的唯一事实源。禁止在 deep_report.py / 任何地方平行重造。
  - 注码/凯利统一走 scripts.bet_core (SSoT, 含 10% 封顶 + 分歧闸门 + 审计)。
  - 模型概率来自 OIP 比分矩阵边缘（由 predict_score 推导）或跨庄共识隐含概率。
  - edge = 模型概率 − 市场隐含概率；EV = 模型概率×赔率 − 1；凯利 = (p·odds−1)/(odds−1)。
  - P0-1: PROD 环境经 value_layer_approved=True 传递审批到 bet_core.safe_stake。
  - P0-3: 热门主动拒止 — odds<1.5 且 edge<3pp → 直接 PASS (不接盘)。

依赖：仅标准库 + scripts.bet_core（已为 SSoT）。无 numpy / 无外部 heavy dep。
"""
from __future__ import annotations
import math
from enum import Enum
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple

# ── 注码/凯利统一走 bet_core (SSoT) ──
# 单注上限 10% 本金直接取自 bet_core.MAX_STAKE_FRAC，杜绝本地副本漂移。
# 若 bet_core 不可用 (缺 PyYAML 等)，直接报错而非静默用过期副本。
try:
    from scripts.bet_core import (
        safe_stake as _bet_core_safe_stake,
        kelly_fraction,
        MAX_STAKE_FRAC as _MAX_STAKE_FRAC,
    )
    _HAS_BET_CORE = True
except Exception as _bet_core_err:  # pragma: no cover
    raise ImportError(
        "compute_value_layer 依赖 scripts.bet_core (注码/凯利单一事实源); "
        "请先安装 PyYAML (requirements.txt: PyYAML>=6.0)."
    ) from _bet_core_err

# ── P1a 跨庄severity分级 (复用 cross_book_edge SSoT; 不可用时本地兜底, 防硬依赖) ──
try:
    from pipeline.cross_book_edge import classify_severity as _cb_severity
except Exception:  # pragma: no cover
    def _cb_severity(pp: float) -> str:
        return "HIGH" if pp >= 15 else "MED" if pp >= 10 else "LOW" if pp >= 5 else ""

# ── P0-3 热门主动拒止配置 ──
# 从 expert_registry.yaml 加载; 失败则用硬编码默认值 (与 bet_core 同源)。
def _load_betting_config() -> dict:
    import yaml, os
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(_root, "config", "expert_registry.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("betting", {})
    except Exception:
        return {}

_BC2 = _load_betting_config()
_HOT_REJECT_ENABLED = bool(_BC2.get("hot_reject_enabled", True))
_HOT_REJECT_ODDS_MAX = float(_BC2.get("hot_reject_odds_max", 1.5))
# 抗诱导口径 (2026-08-18): 判定用隐含概率坐标 (1/odds), 与原始赔率值单调等价、行为不变,
# 但阈值语义跨日稳定(1.5 的"深盘"含义随当日抽水漂移, 66.7% 隐含概率不漂移)。
_HOT_REJECT_MIN_IMPLIED = 1.0 / _HOT_REJECT_ODDS_MAX  # ≈0.667
_HOT_REJECT_EDGE_MIN_PP = float(_BC2.get("hot_reject_edge_min_pp", 3.0))


def _capped_stake(p: float, odds: float, bankroll: float,
                  frac_kelly: float = 0.5, gate: bool = True,
                  source: str = "compute_value_layer") -> float:
    """统一封顶注码 (10%封顶 + 分歧闸门, 走 bet_core.safe_stake SSoT)。
    P0-1: 始终传 value_layer_approved=True (本函数是价值层唯一注码入口)."""
    stake, _ = _bet_core_safe_stake(
        p, odds, bankroll, frac_kelly=frac_kelly, gate=gate, source=source,
        value_layer_approved=True)
    return float(stake)


def market_implied(odds: List[float]) -> List[float]:
    """由 1X2 赔率推导隐含概率（proportional 法剔除抽水）。"""
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [x / s for x in inv]


# ─────────────────────────────────────────────────────────────────────────────
# FLB (favorite-longshot bias) 感知去水 — 单一事实源 (SSoT)
#
# 实证依据 (scripts/verify_favorite_longshot.py, IW 140729 场, 抽水 8.17%):
#   经典 FLB 强确认 — 按赔率十分位 ROI 单调降:
#     热门(赔率中值1.45) ROI -3.1%  →  冷门(赔率中值7.0) ROI -21.9%
#   冷门被系统性高估(实际打出率低于去水隐含概率 -2.5pp),
#   热门被相对低估(高出 +4pp@1.0-1.5). 但单庄内一切负 ROI,
#   FLB 只是"亏得少", 非独立盈利引擎.
# 推论(易错, 已纠正): devig_flb 产出的公平概率对冷门更低/热门更高, 更接近真实.
#   但正因为冷门真实概率比 FLB 公平概率还低, 任何模型若敢给冷门高于市场的 edge,
#   几乎必是被 FLB 偏差带偏 -> 冷门侧 edge 须惩罚, 而非采纳.
# 纯标准库实现(本模块禁 numpy), 与 numpy 版 pipeline/flb_adjust.py 逻辑一致.
# ─────────────────────────────────────────────────────────────────────────────
FLB_GAMMA = 1.08            # 幂变换指数; >1 压缩长赔率(小值)相对权重 -> 热门偏高/冷门偏低
LONGSHOT_THRESHOLD = 3.0    # 赔率 >= 此值视为冷门(长赔率)
LONGSHOT_PENALTY = 0.5      # 冷门侧 edge 惩罚系数(砍半); 抑制伪"冷门价值"

def market_implied_flb(odds: List[float], gamma: float = FLB_GAMMA) -> List[float]:
    """FLB 感知公平概率(纯标准库). 幂变换 r^gamma (r=1/odds);
    gamma>1 -> 公平概率热门偏高/冷门偏低, 更贴真实(见上实证). 和恒为 1."""
    raw = [1.0 / o for o in odds]
    adj = [x ** gamma for x in raw]
    s = sum(adj)
    return [x / s for x in adj]

def flb_edge_penalty(odds: List[float], threshold: float = LONGSHOT_THRESHOLD,
                      penalty: float = LONGSHOT_PENALTY) -> List[float]:
    """逐结果 edge 惩罚系数: 冷门(赔率>=threshold)侧乘 penalty, 否则 1.0.
    用途: 模型敢给冷门高 edge 时抑制(冷门真实比 FLB 公平概率还低), 避免伪价值下注."""
    return [penalty if o >= threshold else 1.0 for o in odds]


def compute_value_layer(
    odds: List[float],
    model_probs: List[float],
    bankroll: float = 10000.0,
    frac_kelly: float = 0.5,
    overround: Optional[float] = None,
    gate: bool = True,
    use_flb: bool = True,
    cross_book: Optional[Dict[str, Any]] = None,
    min_spread_pp: float = 0.0,
) -> Dict[str, Any]:
    """
    价值层主函数 (1X2)。

    参数：
      odds        : [主胜, 平局, 客胜] 十进制赔率
      model_probs : [P主胜, P平, P客胜] 模型概率（OIP矩阵边缘或lambda推导）
      bankroll    : 本金基准（用于算出建议注码绝对额），默认 10000
      frac_kelly  : 凯利比例（0.5=半凯利），默认 0.5
      overround   : 抽水（可选，仅用于展示；不传则按赔率倒数和算）
      gate        : 分歧闸门（True=启用；False=裸下注不闸门对照语义）
      use_flb     : 是否用 FLB 感知公平概率 + 冷门惩罚(默认 True, 更诚实的价值信号)。
                     实证见 market_implied_flb 文档: 冷门被高估, 故公平概率热门偏高、
                     冷门侧 edge 惩罚, 抑制伪"冷门价值"。设 False 退回均匀去水(旧行为)。

    返回：
      { odds, market_implied, model_prob, overround_pct, rows, best_direction,
        best_edge_pct, decision, decision_text, scenario, flb_applied }
    """
    if overround is None:
        overround = (sum(1.0 / o for o in odds) - 1.0)
    # ── P1a 跨庄共识锚定 (2026-08-01, v6铁律: 真edge来自跨庄价差) ──
    # 传入 cross_book(=cross_book_edge.analyze_match 的单场输出)时, 市场公平价改用
    # 跨庄共识(各庄去水概率中位数, 抗离群), 替代单庄去水/FLB; 共识已是公平价, 不再FLB惩罚。
    spread_pp = 0.0
    cb_used = False
    if cross_book:
        cons = cross_book.get("consensus") or {}
        spread_pp = float(cross_book.get("max_spread_pp") or 0.0)
        ch, cd, ca = cons.get("H"), cons.get("D"), cons.get("A")
        if ch and cd and ca and ch > 0 and cd > 0 and ca > 0:
            mk = [float(ch), float(cd), float(ca)]
            penalties = [1.0, 1.0, 1.0]
            cb_used = True
    if not cb_used:
        # FLB 感知公平概率 + 冷门惩罚(默认开); use_flb=False 退回均匀去水(旧行为, 向后兼容)
        if use_flb:
            mk = market_implied_flb(odds)
            penalties = flb_edge_penalty(odds)
        else:
            mk = market_implied(odds)
            penalties = [1.0, 1.0, 1.0]
    outcomes = ["H", "D", "A"]
    rows: List[Dict[str, Any]] = []
    for idx, o in enumerate(odds):
        p_mkt = mk[idx]
        p_mod = model_probs[idx]
        edge = (p_mod - p_mkt) * penalties[idx]   # 冷门侧 edge 经 FLB 惩罚
        ev = p_mod * o - 1.0
        k = kelly_fraction(p_mod, o)
        stake_raw = bankroll * k * frac_kelly
        if cb_used:
            # P1a 跨庄模式: stake_unit 走 bet_core.safe_stake 完整风控(P0a热门限注+跨庄分歧门槛+10%封顶),
            # 把 spread_pp 传入启用跨庄闸门; min_spread_pp>0 时 spread 不足则 stake=0 (真edge不足不接盘)。
            # P0-1: value_layer_approved=True (经 compute_value_layer 审批).
            stake_capped, _ = _bet_core_safe_stake(
                p_mod, o, bankroll, frac_kelly=frac_kelly, gate=gate,
                spread_pp=spread_pp, min_spread_pp=min_spread_pp,
                value_layer_approved=True,
                source="compute_value_layer")
        else:
            # stake_unit 仅供展示; 实际下注须走 bet_core.safe_stake (含 10% 封顶)
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
    # P1a: 加 best.stake_unit>0 — 跨庄模式下 spread 门槛未过则 stake_unit=0, 强制 PASS (不接盘);
    #      非跨庄模式 ev>0 ⟺ stake_unit>0, 不改变原行为。
    positive_ev = best["ev"] > 0 and gate and best["stake_unit"] > 0  # 分歧闸门未过→强制 PASS

    # ── P0-3 热门主动拒止 (2026-08-01) ──
    # odds < 1.5 且 edge < 3pp → 即使 EV>0 也 PASS。
    # 实证: 低赔热门即使有微小+edge, 也会被7-9% overround 吃掉;
    # 热门限注(3%)只能止血, 不能止亏 → 直接拒止, 不接盘。
    hot_rejected = False
    if positive_ev and _HOT_REJECT_ENABLED:
        if (1.0 / best["odds"]) > _HOT_REJECT_MIN_IMPLIED and best["edge_pct"] < _HOT_REJECT_EDGE_MIN_PP:
            positive_ev = False
            hot_rejected = True

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
        if hot_rejected:
            decision_text = (
                f"PASS · 热门拒止(P0-3): odds={best['odds']:.2f}<{_HOT_REJECT_ODDS_MAX} "
                f"edge=+{best['edge_pct']:.2f}%<{_HOT_REJECT_EDGE_MIN_PP}pp, "
                f"微小edge会被overround吃掉, 不接盘"
            )
        else:
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
        "flb_applied": (use_flb and not cb_used),
        # ── P0-3 热门拒止字段 ──
        "hot_rejected": hot_rejected,
        # ── P1a 跨庄字段 ──
        "cross_book_used": cb_used,
        "spread_pp": round(spread_pp, 2),
        "severity": (_cb_severity(spread_pp) if cb_used else ""),
        "edge_basis": ("cross_book_consensus" if cb_used else "single_book_devig"),
        # ── 报告Fix D(3): 单庄/高抽水 edge 可靠性标注 ──
        # 跨庄共识(多庄交叉)→ high; 单庄且抽水高(>8%)→ low(edge 不可靠, 仅作参考);
        # 其余单庄正常抽水 → medium。前端据此降低单庄高抽水信号的权重。
        "reliability": ("high" if cb_used else ("low" if overround > 0.07 else "medium")),
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
    positive_ev = best["ev"] > 0 and gate  # 分歧闸门未过→强制 PASS
    if positive_ev:
        stake = _capped_stake(best["model_prob"], best["best_odds"], bankroll,
                              frac_kelly=frac_kelly, gate=gate, source="value_layer_submarket_legs")
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


# ═══════════════════════════════════════════════════════════════════════════
# OU (大小球) 价值层 — 单一事实源 (SSoT)
#
# 原 deep_report.ou_value 平行重造迁出: 价值层数学 (EV / 凯利 / 注码 / 情景PnL / 决策)
# 只允许在本模块出现。deep_report.ou_value 现为 compute_ou_value 的别名 (re-export)。
#
# 诚实约束 (v6 铁律): 模型 P(大) 由主盘1X2反推的 Poisson 期望进球得出(同源主盘),
# 故不宣称"模型优势", 而是检查"主盘1X2隐含的总进球预期" vs "大小球盘隐含的总进球预期"
# 是否出现跨市场矛盾(gap) → 可下注信号。仅当 |gap|>=gap_threshold 且该侧 EV>0 才下注。
# ═══════════════════════════════════════════════════════════════════════════

def _deoverround(oh: float, od: float, oa: float) -> Tuple[float, float, float]:
    """1X2 去抽水 → 隐含 P(H),P(D),P(A)。纯标准库版(避免依赖 scipy 版 score_model)。"""
    o = 1.0 / oh + 1.0 / od + 1.0 / oa
    return (1.0 / oh) / o, (1.0 / od) / o, (1.0 / oa) / o


def _poisson_pmf(lam: float, k: int) -> float:
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _solve_oip_simple(ph: float, pd_: float, pa: float, maxg: int = 8) -> Tuple[float, float]:
    """纯标准库 OIP λ 求解 (粗网格), 仅 fallback 用; 正常由调用方传 model_m (比分矩阵)。"""
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
    """P(总进球 > line) from 比分概率矩阵 M (numpy 2D 或 list-of-lists, 0-indexed)。"""
    n = len(M)
    p = 0.0
    for i in range(n):
        for j in range(n):
            if i + j > line:
                p += M[i][j]
    return float(p)


def compute_ou_value(oh: float, od: float, oa: float, ou_line: float, over_odds: float, under_odds: float,
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
        ph, mpd, pa = _deoverround(oh, od, oa)
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
                              gate=gate, source="value_layer_ou")
        if stake <= 0:
            # 分歧闸门未过 / kelly<=0 → 不下注
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


# ═══════════════════════════════════════════════════════════════════════════
# 子市场价值层 (平局共识 / 波胆) — 单一事实源 (SSoT)
#
# 原 deep_report.draw_consensus_value / correct_score_value 平行重造迁出:
# 价值层数学 (EV / 凯利 / 注码 / 情景PnL / 决策) 只允许在本模块出现。
# deep_report 现为两者的 re-export 别名 (兼容 bridge_service / backfill_bet_records /
# calibrate_wc_cs)。
#
# 诚实约束 (v6 铁律): OIP 矩阵 λ 同源主盘, 故子市场 edge 只来自
#   (a) 跨市场不一致: 主盘1X2隐含总进球 vs 大小球盘隐含总进球 矛盾
#   (b) 跨庄共识溢价: ≥2家独立庄家(或WH×IW)对平局/波胆的定价 > 主盘同源定价
# 绝不"模型 vs 同源盘"循环论证。
# ═══════════════════════════════════════════════════════════════════════════

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
    _, mkt_pd, _ = _deoverround(primary_oh, primary_od, primary_oa)
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
                         "fair_decimal": round(1 / p, 2) if p > 0 else None,
                         "fair_eff_decimal": round(1 / p_eff, 2) if p_eff > 0 else None,
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


# ═══════════════════════════════════════════════════════════════════════════
# 事故⑦ 双源 ROI 治理 (REQ-09, T09 + T13)
#   - 写入护栏: 脏行拒绝写 (呼应"修bug必追问历史数据回填", 严禁脏数据入 unified_history.db)
#   - 双源 ROI 对齐: 同 (match, market, timestamp) 对齐两源, 算置信区间;
#     |ΔROI| > Config.roi_delta_threshold(默认5.0pp) → source=DISPUTED
#   - 每个信号/ROI 输出带 source + confidence
# ═══════════════════════════════════════════════════════════════════════════

class Source(str, Enum):
    """ROI/信号来源 (REQ-13)。"""
    LEYU = "LEYU"
    LEISU = "LEISU"
    UNIFIED = "UNIFIED"
    DISPUTED = "DISPUTED"


@dataclass
class SignalOutput:
    """统一信号输出 (REQ-13: 必带 source + confidence)。"""
    match_id: str
    signal_type: str
    value: float = 0.0
    roi: float = 0.0
    source: Source = Source.UNIFIED
    confidence: float = 0.0
    verdict: str = "NEUTRAL"
    basis: str = ""


# 脏行 ROI 超界阈值 (pp); 远超合理 ROI 区间者视为占位/垃圾
_ROI_OUTLIER_PP = 1_000_000.0


def validate_roi_record(rec: Dict[str, Any]) -> Tuple[bool, str]:
    """脏行写入护栏 (事故⑦): 返回 ``(ok, reason)``。

    拒绝以下脏数据写入 ``unified_history.db``:
      - 缺失关键字段 (``match_id/market/timestamp/roi`` 为 None 或空串)
      - ``roi`` 非有限数 / 非数值
      - ``roi`` 超界 (``|roi| > _ROI_OUTLIER_PP``, 如 99999 占位)
      - ``source`` 非法 (None 或不在已知源集合)
      - 赔率 ``odds`` 存在但 ``<= 0``

    注意: 本护栏只判"脏", 不判"赛果对错"; 真实赛果数据由 ``clean_outcomes`` SSoT 管理,
    严禁在此改动/直读 ``match_outcomes`` 盘口列 (铁律)。
    """
    if not isinstance(rec, dict):
        return False, "not_a_dict"
    required = ("match_id", "market", "timestamp", "roi")
    for k in required:
        v = rec.get(k)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return False, f"missing_field:{k}"
    try:
        roi = float(rec["roi"])
    except (TypeError, ValueError):
        return False, "roi_not_numeric"
    if not math.isfinite(roi):
        return False, "roi_not_finite"
    if abs(roi) > _ROI_OUTLIER_PP:
        return False, "roi_out_of_range"
    source = rec.get("source")
    if not source or str(source) not in {s.value for s in Source}:
        return False, "bad_source"
    odds = rec.get("odds")
    if odds is not None:
        try:
            if float(odds) <= 0:
                return False, "nonpositive_odds"
        except (TypeError, ValueError):
            return False, "odds_not_numeric"
    return True, ""


def _roi_threshold_from_config() -> float:
    """从中央配置读取双源 ROI 偏差阈值(pp); 不可用时回落 5.0 (TBC-3 默认值)。"""
    try:
        from core.config import get_config as _get_cfg
        cfg = _get_cfg()
        if cfg is not None:
            return float(cfg.roi_delta_threshold)
    except Exception:
        pass
    return 5.0


def align_dual_source_roi(
    rows: List[Dict[str, Any]],
    roi_delta_threshold: Optional[float] = None,
    confidence_z: float = 1.96,
) -> List[Dict[str, Any]]:
    """双源 ROI 对齐 (事故⑦)。

    输入 ``rows``: 混合两源 (LEYU/LEISU) 的 ROI 样本, 每条含
        ``match_id, market, timestamp, roi, source, n``(可选样本量, 默认1)
    ROI 单位 = pp (与设计文档阈值 ``roi_delta_threshold=5.0pp`` 一致)。

    逻辑:
      1. 按 ``(match_id, market, timestamp)`` 对齐两源样本。
      2. 两源齐全 → 算 ``ΔROI = roi_leiyu - roi_leisu`` 与 95% 置信区间
         (独立两样本 ``SE = sqrt(se_a^2 + se_b^2)``, ``se = |roi|/sqrt(n)``)。
      3. ``|ΔROI| > roi_delta_threshold``(默认 ``Config.roi_delta_threshold=5.0pp``)
         → ``source=DISPUTED/已知采样差异``, ``confidence`` 随偏差降低。
      4. 仅单源 → 不标 DISPUTED, 直接给该源(置信度按样本量给中低)。

    返回: 对齐后的信号列表 (每个带 ``source`` + ``confidence`` + ``verdict`` + ``basis``)。
    """
    if roi_delta_threshold is None:
        roi_delta_threshold = _roi_threshold_from_config()
    groups: Dict[Tuple[Any, Any, Any], Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        key = (r.get("match_id"), r.get("market"), r.get("timestamp"))
        src = str(r.get("source") or "")
        groups.setdefault(key, {})[src] = r

    aligned: List[Dict[str, Any]] = []
    for key, by_src in groups.items():
        rec_a = by_src.get(Source.LEYU.value)
        rec_b = by_src.get(Source.LEISU.value)
        if not rec_a or not rec_b:
            # 仅单源: 不标 DISPUTED, 直接给该源(置信度随样本量上升, 上限低于双源一致)
            only = rec_a or rec_b
            roi = float(only.get("roi", 0.0))
            n = max(int(only.get("n", 1) or 1), 1)
            confidence = round(min(0.35 + 0.05 * math.log10(n + 1), 0.6), 4)
            aligned.append({
                "match_id": key[0], "market": key[1], "timestamp": key[2],
                "roi": round(roi, 4),
                "source": str(only.get("source")),
                "confidence": confidence,
                "verdict": "NEUTRAL",
                "delta_roi": 0.0, "ci95": [0.0, 0.0], "disputed": False,
                "basis": "单源样本, 无双源对照, 不标 DISPUTED",
            })
            continue

        roi_a = float(rec_a.get("roi", 0.0))
        roi_b = float(rec_b.get("roi", 0.0))
        n_a = max(int(rec_a.get("n", 1) or 1), 1)
        n_b = max(int(rec_b.get("n", 1) or 1), 1)
        delta = roi_a - roi_b
        se_a = abs(roi_a) / math.sqrt(n_a)
        se_b = abs(roi_b) / math.sqrt(n_b)
        se = math.sqrt(se_a * se_a + se_b * se_b)
        lo = delta - confidence_z * se
        hi = delta + confidence_z * se
        disputed = abs(delta) > roi_delta_threshold
        mean_roi = (roi_a + roi_b) / 2.0
        if disputed:
            # 偏差超阈值: 标 DISPUTED, 置信度随 |Δ|/阈值 倍数下降(下限0.1)
            ratio = abs(delta) / max(roi_delta_threshold * 2.0, 1e-9)
            confidence = round(max(0.1, 1.0 - min(ratio, 1.0)), 4)
            basis = (f"双源ROI偏差 |Δ|={abs(delta):.2f}pp > 阈值{roi_delta_threshold:.1f}pp, "
                     f"标 DISPUTED/已知采样差异 (CI95 [{lo:.2f},{hi:.2f}])")
        else:
            # 双源一致: 高置信, 标 UNIFIED
            confidence = round(min(0.95, 0.7 + 0.1 * math.log10(n_a + n_b)), 4)
            basis = (f"双源ROI一致 (Δ={delta:+.2f}pp 在阈值{roi_delta_threshold:.1f}pp 内, "
                     f"CI95 [{lo:.2f},{hi:.2f}])")
        aligned.append({
            "match_id": key[0], "market": key[1], "timestamp": key[2],
            "roi": round(mean_roi, 4),
            "source": Source.DISPUTED.value if disputed else Source.UNIFIED.value,
            "confidence": confidence,
            "verdict": "DISPUTED" if disputed else "ALIGNED",
            "delta_roi": round(delta, 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "disputed": disputed,
            "basis": basis,
        })
    return aligned
