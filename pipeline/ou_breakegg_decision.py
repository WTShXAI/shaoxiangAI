"""
ou_breakegg_decision.py — 滚球 OU(大小球)破蛋 决策/护栏层  (哨响AI)
================================================================
站在现有 SSoT 之上, 补齐"破蛋信号 → 真下注"之间缺失的**决策护栏**:

  - analysis/live_goal_probe.py : 滚球破蛋概率仪 (probe_match / 开盘OU锚定表 _HT_BREAKEGG_BY_OU /
                                   anchor_gap_signal 开盘→活盘过度收缩漂移信号). 已由 bridge_service 加载.
  - scripts/bet_core.py         : 凯利/风控 SSoT (safe_stake / kelly_fraction / 价值闸门/回撤/热门限注)
  - pipeline/cs_ev_decision.py  : 本模块的 CS 孪生层 (同构护栏)

现有破蛋端点(/api/live-goal-probe)只出概率/方向, 本层补四件事(与 CS 层同构):

  1. OU 市场去抽水 (devig): over/under 赔率各含 margin, 裸比会误报 +EV. 先去抽水再比.
  2. 跨庄/漂移证据闸门: 无跨庄分歧证据 且 无 开盘→活盘漂移证据(anchor_gap_signal 过度收缩) →
     即使 +EV 也只能 "observe"(观察), 不升级为 bet. 铁律: 单图无开盘价不升级确定性下注.
  3. obscure 联赛强制只观察: obscure_league=True → 所有信号 verdict=observe, 绝不自动 BET.
  4. 半凯利封顶: 经 bet_core.safe_stake 统一注码, 复用价值闸门/回撤/热门限注, 不另立注码逻辑.

OU 业务规则 (内化自已定稿的滚球破蛋神器规则):
  - 全场: 硬编码锚定线 {2.0, 2.25, 2.5} (select_ou_lines 在可用盘中取这些线)
  - 半场(is_halftime): 按 max-Δ 选线 — 取 |model_over_prob - implied_over| 最大的线

输入: OU 市场(单线 {line,over,under}) + 开盘/活盘总球 + live 状态 + 联赛 + 模型大球概率 + 证据标志.
输出: {over:{verdict, fair_prob, implied_prob, divergence, stake, kelly},
        under:{...}, bettable, evidence, obscure_league}

诚实声明: 本层是"纪律化 +EV 过滤器 + 护栏", 非预测神器。实盘前必须 paper-trading 验证。
"""
from __future__ import annotations

import os
import sys
import math
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.cs_ev_engine import league_goal_rate  # 联赛场均进球(泊松基线)
try:
    from scripts.bet_core import safe_stake, kelly_fraction  # 凯利/风控 SSoT
except Exception as e:  # pragma: no cover
    logger.warning(f"[ou_breakegg_decision] bet_core import failed: {e}; kelly disabled")
    safe_stake = None
    kelly_fraction = None


# ── OU 业务规则: 全场锚定线 ──
FULLTIME_ANCHOR_LINES = (2.0, 2.25, 2.5)


# ════════════════════════════════════════════════════════════════
# 1. OU 线选择 (内化业务规则)
# ════════════════════════════════════════════════════════════════
def select_ou_lines(candidate_lines: List[Dict[str, float]],
                    is_halftime: bool = False,
                    model_probs: Optional[Dict[float, float]] = None,
                    implied_probs: Optional[Dict[float, float]] = None) -> List[Dict[str, float]]:
    """按业务规则选 OU 线。

    candidate_lines: [{'line':2.5,'over':1.83,'under':2.07}, ...]
    is_halftime=False (全场): 取锚定线 {2.0,2.25,2.5} 中可用的。
    is_halftime=True  (半场): 按 max-Δ 选线 — 取 |model_over - implied_over| 最大的单线。
    model_probs/implied_probs: 可选, {line: prob} 用于 max-Δ 计算。
    """
    if not candidate_lines:
        return []
    if not is_halftime:
        out = [c for c in candidate_lines if float(c["line"]) in FULLTIME_ANCHOR_LINES]
        # 锚定线不在候选(主盘偏离)则回退全部 —— 下游按"去水偏离最大"挑线,
        # 这是找庄家定价偏差的 edge 来源(回测: 命中率56.1%/ROI+18.9% vs 跟随主盘50.8%/+2.7%)。
        # 切勿改成"去水最均衡(跟随主盘)": 那会放弃 edge 跟随市场, 已回测证伪。
        return out if out else candidate_lines
    # 半场: max-Δ 选线
    best = None
    best_d = -1.0
    for c in candidate_lines:
        ln = float(c["line"])
        mp = (model_probs or {}).get(ln)
        ip = (implied_probs or {}).get(ln)
        if mp is None or ip is None:
            continue
        d = abs(mp - ip)
        if d > best_d:
            best_d, best = d, c
    return [best] if best else candidate_lines


# ════════════════════════════════════════════════════════════════
# 2. 泊松大球概率 (模型基线, 可被调用方覆盖)
# ════════════════════════════════════════════════════════════════
def _poisson_cdf(lam: float, k: int) -> float:
    """P(X <= k) 累积, 用 log-gamma 递推避免溢出。"""
    if lam <= 0:
        return 1.0
    p = math.exp(-lam)
    cdf = p
    for i in range(1, k + 1):
        p *= lam / i
        cdf += p
    return min(1.0, max(0.0, cdf))


def poisson_over_prob(rate_total: float, line: float) -> float:
    """P(总进球 > line) 的泊松近似 (rate_total=每场期望总球)。"""
    if rate_total <= 0:
        return 0.0
    # line 可能是 .25/.75 → 用最近整数边界扣分 (简化: 取 floor(line) 为"已超过"阈值)
    k = int(math.floor(line))
    return round(1.0 - _poisson_cdf(rate_total, k), 4)


def over_prob_from_remaining(lam_remaining: float, current_total_goals: float, line: float) -> float:
    """P(终场总球 > line) 给定剩余进球期望 lam_remaining 与当前已进总球。

    剩余进球 ~ Poisson(lam_remaining); 线已被超过(current_total_goals > line)→ 1.0。
    用于把"剩余期望"翻译成 OU 大球概率(比全场泊松更 live 状态感知)。
    """
    if lam_remaining <= 0:
        return 1.0 if current_total_goals > line else 0.0
    need = line - current_total_goals  # 还需进多少球才超线
    if need <= 0:
        return 1.0
    k = int(math.floor(need))  # 需 G >= k+1 才超 line
    cdf = _poisson_cdf(lam_remaining, k)
    return round(1.0 - cdf, 4)


def derive_model_over_prob(line: float, current_home: int = 0, current_away: int = 0,
                           expected_home: Optional[float] = None, expected_away: Optional[float] = None,
                           current_total: Optional[float] = None, league: Optional[str] = None) -> float:
    """模型大球概率(多源回退, 优先级自上而下):
      1) probe_match 的 1X2 去水拟合期望进球(expected_home/away) → 剩余期望 λ=(eh+ea)-(ch+ca)
      2) 市场隐含剩余(current_total - 已进总球)
      3) 联赛泊松基线(全场)
    """
    cg = float(current_home + current_away)
    if expected_home is not None and expected_away is not None:
        lam = max(0.0, (float(expected_home) + float(expected_away)) - cg)
        return over_prob_from_remaining(lam, cg, line)
    if current_total is not None:
        lam = max(0.0, float(current_total) - cg)
        # 兜底: 剩余期望≤0 且尚未超线(含压线) → 回退联赛泊松基线, 杜绝返回0
        # (早期0-0等"已进=隐含期望=0"场景; 已超线时 over_prob_from_remaining 正确返回1.0)
        if lam <= 0 and cg <= line:
            rh, ra = league_goal_rate(league=league)
            return poisson_over_prob(rh + ra, line)
        return over_prob_from_remaining(lam, cg, line)
    rh, ra = league_goal_rate(league=league)
    return poisson_over_prob(rh + ra, line)


# ════════════════════════════════════════════════════════════════
# 3. 决策层: devig + 证据闸门 + obscure 护栏 + 半凯利封顶
# ════════════════════════════════════════════════════════════════
def decide_ou(opening_total: Optional[float], current_total: Optional[float],
              ou_market: Dict[str, float], minute: int = 0, league: Optional[str] = None,
              model_over_prob: Optional[float] = None, equity: float = 3000.0,
              thresh: float = 0.02, obscure_league: bool = False,
              cross_book_evidence: bool = False, drift_evidence: bool = False,
              require_evidence: bool = True, is_halftime: bool = False) -> Dict:
    """滚球 OU 破蛋 决策/护栏入口。

    Args:
        opening_total / current_total : 开盘/活盘 去水隐含总球 (用于 drift 上下文, 非本层计算)
        ou_market     : {'line':2.5, 'over':1.83, 'under':2.07}
        minute        : 当前比赛分钟
        league        : 联赛名 (泊松基线 + obscure 判定上下文)
        model_over_prob : 模型大球概率 (调用方取自 probe_match / 锚定表 / 漂移调整后).
                          None → 用联赛泊松基线自动估算 (透明回退, 无 edge).
        equity        : 当前本金 (凯利注码)
        thresh        : +EV 阈值 (divergence pp)
        obscure_league: True → 强制只观察
        cross_book_evidence: 跨庄分歧证据 (可升级 bet)
        drift_evidence     : 开盘→活盘过度收缩漂移证据 (anchor_gap_signal overshrink, 可升级 bet)
        require_evidence   : True → 无跨庄且无漂移则只能 observe
        is_halftime        : 半场模式 (影响 line 选择与基线)

    Returns:
        {line, over:{...}, under:{...}, bettable, evidence, obscure_league, n_bet}
    """
    try:
        line = float(ou_market["line"])
        over_o = float(ou_market["over"])
        under_o = float(ou_market["under"])
    except (KeyError, TypeError, ValueError):
        return {"error": "ou_market 需含 line/over/under", "verdicts": {}}

    if over_o <= 1.0 or under_o <= 1.0:
        return {"error": "over/under 赔率须 > 1", "verdicts": {}}

    # devig
    inv_o, inv_u = 1.0 / over_o, 1.0 / under_o
    ov = inv_o + inv_u
    if ov <= 0:
        return {"error": "invalid OU odds", "verdicts": {}}
    impl_o = inv_o / ov
    impl_u = inv_u / ov

    # 模型大球概率: 调用方给则用, 否则联赛泊松基线
    if model_over_prob is None:
        rh, ra = league_goal_rate(league=league)
        model_over_prob = poisson_over_prob(rh + ra, line)

    # 证据闸门
    bettable = (not require_evidence) or cross_book_evidence or drift_evidence
    if obscure_league:
        bettable = False

    def _side(p_model: float, p_impl: float, odds: float, name: str) -> Dict:
        div = p_model - p_impl
        is_ev = div >= thresh
        verdict = "reject"
        stake, kelly = 0.0, 0.0
        if is_ev:
            if bettable:
                verdict = "bet"
                if safe_stake is not None:
                    # bettable 已在决策层判定, 此处越过 safe_stake 的 gate 默认 False
                    stake, kelly = safe_stake(p=p_model, o=odds, equity=equity,
                                             gate=True, value_layer_approved=True,
                                             source=f"ou:{name}:{line}")
            else:
                verdict = "observe"
        return {
            "name": name, "odds": odds,
            "fair_prob": round(p_model, 4),
            "implied_prob": round(p_impl, 4),
            "divergence": round(div, 4),
            "is_ev": is_ev,
            "verdict": verdict,
            "stake": round(stake, 2),
            "kelly": round(kelly, 4),
        }

    over_side = _side(model_over_prob, impl_o, over_o, "over")
    under_side = _side(1.0 - model_over_prob, impl_u, under_o, "under")

    n_bet = sum(1 for s in (over_side, under_side) if s["verdict"] == "bet")

    # ── 极简结果(只给结论, 运算沉到 over/under) ──
    bet_side = over_side if over_side["verdict"] == "bet" else (
        under_side if under_side["verdict"] == "bet" else None)
    if bet_side:
        _reason = ("开盘→活盘过度收缩(漂移)" if drift_evidence
                   else ("跨庄分歧" if cross_book_evidence else "已授权下注"))
        _action = f"买{bet_side['name'].upper()} {line} @{bet_side['odds']}"
        _verdict = "bet"
    elif obscure_league:
        _reason, _action, _verdict = "冷门联赛强制观察", "观察", "observe"
    elif not bettable:
        _reason, _action, _verdict = "无跨庄/漂移证据", "观察", "observe"
    else:
        _reason, _action, _verdict = "有证据但无+EV", "不操作", "no_action"
    result = {
        "verdict": _verdict,
        "action": _action,
        "side": bet_side["name"] if bet_side else None,
        "line": line,
        "odds": bet_side["odds"] if bet_side else None,
        "stake": bet_side["stake"] if bet_side else 0.0,
        "reason": _reason,
    }

    return {
        "line": line,
        "is_halftime": is_halftime,
        "overround": round(ov, 4),
        "league": league,
        "obscure_league": obscure_league,
        "bettable": bettable,
        "evidence": {"cross_book": cross_book_evidence, "drift": drift_evidence},
        "n_bet": n_bet,
        "over": over_side,
        "under": under_side,
        "result": result,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("\n══════════ 场景A: 全场 OU2.5, 关证据闸门(看裸+EV) · 无漂移模型基线 ══════════")
    a = decide_ou(opening_total=2.6, current_total=2.5,
                  ou_market={"line": 2.5, "over": 1.83, "under": 2.07},
                  minute=60, league="世界杯2026", require_evidence=False)
    print(f"overround={a.get('overround')} n_bet={a.get('n_bet')}")
    print(f"  OVER  {a['over']}")
    print(f"  UNDER {a['under']}")

    print("\n══════════ 场景B: 同场 · 开证据闸门(无跨庄/无漂移 → +EV 只能 observe) ══════════")
    b = decide_ou(opening_total=2.6, current_total=2.5,
                  ou_market={"line": 2.5, "over": 1.83, "under": 2.07},
                  minute=60, league="世界杯2026", require_evidence=True,
                  cross_book_evidence=False, drift_evidence=False)
    print(f"bettable={b.get('bettable')} n_bet={b.get('n_bet')} over.verdict={b['over']['verdict']}")

    print("\n══════════ 场景C: 开盘4.75→活盘3.25 过度收缩(drift=True) + 模型大球上调0.62 → 大球可bet ══════════")
    c = decide_ou(opening_total=4.75, current_total=3.25,
                  ou_market={"line": 2.5, "over": 1.83, "under": 2.07},
                  minute=70, league="世界杯2026", require_evidence=True,
                  drift_evidence=True, model_over_prob=0.62, equity=3000.0)
    print(f"bettable={c.get('bettable')} n_bet={c.get('n_bet')} over.verdict={c['over']['verdict']} stake={c['over']['stake']}")

    print("\n══════════ 场景D: obscure 联赛 + drift → 强制 observe(绝不自动BET) ══════════")
    d = decide_ou(opening_total=4.75, current_total=3.25,
                  ou_market={"line": 2.5, "over": 1.83, "under": 2.07},
                  minute=70, league="乌兹别克超", obscure_league=True,
                  drift_evidence=True, model_over_prob=0.62)
    print(f"obscure={d.get('obscure_league')} bettable={d.get('bettable')} n_bet={d.get('n_bet')} over.verdict={d['over']['verdict']}")

    print("\n══════════ 场景E: select_ou_lines 业务规则 ══════════")
    cands = [{"line": 1.5, "over": 1.9, "under": 1.9}, {"line": 2.0, "over": 1.85, "under": 2.0},
             {"line": 2.25, "over": 1.8, "under": 2.05}, {"line": 2.5, "over": 1.83, "under": 2.07},
             {"line": 3.5, "over": 2.1, "under": 1.7}]
    print("  全场锚定线:", [c["line"] for c in select_ou_lines(cands, is_halftime=False)])
    mp = {1.5: 0.7, 2.0: 0.6, 2.25: 0.55, 2.5: 0.48, 3.5: 0.3}
    ip = {1.5: 0.53, 2.0: 0.54, 2.25: 0.55, 2.5: 0.506, 3.5: 0.59}
    ht = select_ou_lines(cands, is_halftime=True, model_probs=mp, implied_probs=ip)
    print(f"  半场 max-Δ 选线: {[c['line'] for c in ht]} (Δ最大处)")
