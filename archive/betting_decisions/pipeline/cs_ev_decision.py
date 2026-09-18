"""
cs_ev_decision.py — CS(波胆) +EV 决策/护栏层  (哨响AI)
================================================
站在现有 SSoT 之上, 补齐"算 divergence → 真下注"之间缺失的**决策护栏**:

  - pipeline/cs_ev_engine.py : 滚球 CS +EV 检测器 (泊松剩余时间模型 / cs_value_flag / rank_ev_scores)
  - scripts/bet_core.py      : 凯利/风控单一事实源 (safe_stake / kelly_fraction / 价值闸门/回撤/热门限注/跨庄 spread)

现有 cs_ev_engine 只"算 divergence 打标签", 本层补四件事:

  1. CS 市场去抽水 (devig): 现有引擎用裸 1/odds 作隐含概率(偏乐观, 把抽水算进模型边),
     本层先去抽水再比, +EV 判定更诚实。
  2. 跨庄/漂移证据闸门: 无跨庄分歧证据 且 无 开盘→滚盘漂移证据 → 即使 +EV 也只能 "observe"(观察),
     不升级为 bet。铁律: 单张 live 截图无开盘价时, 低水线只标"庄家倾向", 不升级为确定性下注。
  3. obscure 联赛强制只观察: obscure_league=True → 所有信号 verdict=observe, 绝不自动 BET
     (obscure 置信度低, 领先后收缩防守假设脆弱, 见铁律)。
  4. 半凯利封顶: 经 bet_core.safe_stake 统一注码, 复用价值闸门/回撤/热门限注, 不另立注码逻辑。

输入: CS 市场(支持 odds_db JSON 嵌套结构 或 扁平 {"2-1":7.9}) + live 状态 + 联赛 + 证据标志。
输出: 每条比分的 {verdict: bet|observe|reject, fair_prob, implied_prob, divergence, stake, kelly} + 汇总。

诚实声明: 本层是"纪律化 +EV 过滤器 + 护栏", 非预测神器。实盘前必须 paper-trading 验证。
"""
from __future__ import annotations

import os
import sys
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── 依赖现有 SSoT (不重复实现泊松/凯利) ──
from pipeline.cs_ev_engine import cs_value_flag, league_goal_rate

try:
    from scripts.bet_core import safe_stake, kelly_fraction  # 凯利/风控 SSoT
except Exception as e:  # pragma: no cover
    logger.warning(f"[cs_ev_decision] bet_core import failed: {e}; kelly disabled")
    safe_stake = None
    kelly_fraction = None


# ════════════════════════════════════════════════════════════════
# 1. CS 市场扁平化: 支持 odds_db JSON 嵌套 / 扁平 dict
# ════════════════════════════════════════════════════════════════
def flatten_cs_market(cs_block: Any) -> Dict[str, float]:
    """把 CS 市场转成 {'2-1': 7.9, ...}。

    支持:
      - 扁平 dict: {'2-1': 7.9, ...}
      - odds_db 嵌套: {'fulltime': {'argentina_win':[{score,odds}], 'draw':[...], 'switzerland_win':[...]}}
                     或 {'argentina_win':[...], 'draw':[...], 'switzerland_win':[...]} (已取 fulltime/halftime)
    """
    out: Dict[str, float] = {}
    if isinstance(cs_block, dict):
        # 含 fulltime/halftime 子结构 → 递归取 fulltime
        if any(k in cs_block for k in ("fulltime", "halftime")):
            sub = cs_block.get("fulltime") or cs_block.get("halftime")
            if sub:
                return flatten_cs_market(sub)
        # 按胜平负类别嵌套: value = list of {score, odds}
        if any(isinstance(v, list) for v in cs_block.values()):
            for _cat, lst in cs_block.items():
                if isinstance(lst, list):
                    for it in lst:
                        if isinstance(it, dict) and "score" in it and "odds" in it:
                            o = it["odds"]
                            if isinstance(o, (int, float)) and o > 1.0:
                                out[str(it["score"])] = float(o)
            return out
        # 已经是扁平 dict
        return {k: float(v) for k, v in cs_block.items()
                if isinstance(v, (int, float)) and v > 1.0}
    return out


# ════════════════════════════════════════════════════════════════
# 2. CS 市场去抽水 (devig)
# ════════════════════════════════════════════════════════════════
def devig_cs_market(market: Dict[str, float]) -> Tuple[Dict[str, float], float]:
    """对 CS 市场去抽水, 返回 (deoverrounded_implied: {score: prob}, overround)。"""
    if not market:
        return {}, 0.0
    inv = {s: 1.0 / o for s, o in market.items()}
    ov = sum(inv.values())
    if ov <= 0:
        return {s: 0.0 for s in market}, 0.0
    return {s: p / ov for s, p in inv.items()}, ov


# ════════════════════════════════════════════════════════════════
# 3. 决策层: 去抽水 + 证据闸门 + obscure 护栏 + 半凯利封顶
# ════════════════════════════════════════════════════════════════
def decide_cs(
    cs_block: Any,
    cur_h: int = 0, cur_a: int = 0, minutes_played: float = 0.0,
    league: Optional[str] = None,
    rate_home: Optional[float] = None, rate_away: Optional[float] = None,
    equity: float = 3000.0,
    thresh: float = 0.02,
    obscure_league: bool = False,
    cross_book_evidence: bool = False,
    drift_evidence: bool = False,
    require_evidence: bool = True,
    value_layer_approved: bool = True,
    top_n: int = 10,
) -> Dict:
    """CS +EV 决策/护栏入口。

    Args:
        cs_block            : CS 市场 (odds_db 嵌套 或 扁平 dict)
        cur_h/cur_a         : 当前(实时)比分 (赛前传 0,0)
        minutes_played      : 已踢分钟数 (赛前传 0.0)
        league              : 联赛名 (用于取主客场均进球率; 未知回退全局)
        rate_home/away      : 可显式覆盖场均率 (否则从 events.db/全局取)
        equity              : 当前本金 (用于凯利注码)
        thresh              : +EV 判定阈值 (divergence pp)
        obscure_league      : True → 强制只观察, 绝不自动 BET
        cross_book_evidence : 有跨庄分歧证据 (可升级为 bet)
        drift_evidence      : 有 开盘→滚盘 漂移证据 (可升级为 bet)
        require_evidence    : True → 无跨庄且无漂移证据则只能 observe
        value_layer_approved: 经价值层审批 (PROD 必需; 本层即 CS 价值层, 默认 True)
        top_n               : 返回的 top-N 比分

    Returns:
        {overround, league, obscure_league, bettable, evidence,
         n_total, n_bet, n_observe, verdicts:[...]}
        verdict ∈ {bet, observe, reject}
    """
    market = flatten_cs_market(cs_block)
    if not market:
        return {"error": "empty CS market", "verdicts": []}

    implied, overround = devig_cs_market(market)
    if rate_home is None or rate_away is None:
        rate_home, rate_away = league_goal_rate(league=league)

    # 证据闸门: 无跨庄 且 无漂移 → 只能 observe (铁律: 单图无开盘价不升级确定性下注)
    bettable = (not require_evidence) or cross_book_evidence or drift_evidence
    if obscure_league:
        bettable = False  # obscure 强制只观察

    verdicts: List[Dict] = []
    for sc, odds in market.items():
        r = cs_value_flag(cur_h, cur_a, minutes_played, sc, odds, rate_home, rate_away, thresh)
        if "error" in r:
            continue
        fair = r["fair_prob"]
        impl = implied.get(sc, 0.0)
        div = fair - impl
        is_ev = div >= thresh

        verdict = "reject"
        stake, kelly = 0.0, 0.0
        if is_ev:
            if bettable:
                verdict = "bet"
                if safe_stake is not None:
                    # bettable 已在决策层判定(证据闸门/obscure 护栏), 此处越过 safe_stake 的 gate 默认 False
                    stake, kelly = safe_stake(
                        p=fair, o=odds, equity=equity, gate=True,
                        value_layer_approved=value_layer_approved,
                        source=f"cs:{sc}",
                    )
            else:
                verdict = "observe"  # 有 +EV 但缺证据 / obscure → 只观察

        verdicts.append({
            "score": sc,
            "odds": odds,
            "fair_prob": round(fair, 4),
            "implied_prob": round(impl, 4),
            "divergence": round(div, 4),
            "is_ev": is_ev,
            "verdict": verdict,
            "stake": round(stake, 2),
            "kelly": round(kelly, 4),
        })

    verdicts.sort(key=lambda x: -x["divergence"])
    n_bet = sum(1 for v in verdicts if v["verdict"] == "bet")
    n_obs = sum(1 for v in verdicts if v["verdict"] == "observe")

    # ── 极简结果(只给结论, 运算沉到 verdicts) ──
    bets = [v for v in verdicts if v["verdict"] == "bet"]
    if bets:
        top = bets[0]
        _action = f"买 {top['score']} @{top['odds']}"
        _verdict = "bet"
    elif obscure_league:
        _action, _verdict = "观察(冷门联赛)", "observe"
    elif not bettable:
        _action, _verdict = "观察(无证据)", "observe"
    else:
        _action, _verdict = "不操作(无+EV)", "no_action"
    result = {
        "verdict": _verdict,
        "action": _action,
        "top_bets": [{"score": v["score"], "odds": v["odds"], "stake": v["stake"]}
                     for v in bets[:3]],
        "bettable": bettable,
        "n_bet": n_bet,
        "n_observe": n_obs,
    }

    return {
        "overround": round(overround, 4),
        "league": league,
        "obscure_league": obscure_league,
        "bettable": bettable,
        "evidence": {"cross_book": cross_book_evidence, "drift": drift_evidence},
        "n_total": len(verdicts),
        "n_bet": n_bet,
        "n_observe": n_obs,
        "verdicts": verdicts[:top_n],
        "result": result,
    }


def decide_cs_from_json(
    json_path: str,
    cur_h: int = 0, cur_a: int = 0, minutes_played: float = 0.0,
    obscure_league: bool = False,
    cross_book_evidence: bool = False,
    drift_evidence: bool = False,
    require_evidence: bool = True,
    equity: float = 3000.0,
    thresh: float = 0.02,
    top_n: int = 10,
) -> Dict:
    """从 odds_db JSON 直接跑决策 (读取 odcs_cs / competition / post_review)。"""
    with open(json_path, "r", encoding="utf-8") as f:
        rec = json.load(f)
    cs = rec.get("odcs_cs")
    if not cs:
        return {"error": "no odcs_cs in record", "verdicts": []}
    league = rec.get("competition")
    out = decide_cs(
        cs, cur_h=cur_h, cur_a=cur_a, minutes_played=minutes_played,
        league=league, equity=equity, thresh=thresh,
        obscure_league=obscure_league,
        cross_book_evidence=cross_book_evidence,
        drift_evidence=drift_evidence,
        require_evidence=require_evidence,
        top_n=top_n,
    )
    out["match"] = rec.get("match_id")
    out["home"] = rec.get("home")
    out["away"] = rec.get("away")
    if "post_review" in rec:
        out["actual"] = rec["post_review"].get("actual_score")
        out["actual_outcome"] = rec["post_review"].get("actual_outcome")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    SAMPLE = os.path.join(_ROOT, "odds_db", "argentina_vs_switzerland_20260712_semifinal.json")

    print("\n══════════ 场景A: 赛前 0-0@0' · WC半决赛(非obscure) · 关证据闸门(看裸+EV) ══════════")
    a = decide_cs_from_json(SAMPLE, cur_h=0, cur_a=0, minutes_played=0.0,
                            obscure_league=False, require_evidence=False)
    print(f"match={a.get('match')} overround={a.get('overround')} "
          f"n_total={a.get('n_total')} n_bet={a.get('n_bet')} n_observe={a.get('n_observe')}")
    print(f"actual={a.get('actual')} ({a.get('actual_outcome')})")
    for v in a.get("verdicts", []):
        print(f"  {v['score']:>4} @ {v['odds']:>6}  fair={v['fair_prob']:.3f} "
              f"impl={v['implied_prob']:.3f} div={v['divergence']:+.3f} "
              f"→ {v['verdict']}  stake={v['stake']}")

    print("\n══════════ 场景B: 同场 · 开证据闸门(无跨庄/无漂移 → +EV 也只能 observe) ══════════")
    b = decide_cs_from_json(SAMPLE, cur_h=0, cur_a=0, minutes_played=0.0,
                            obscure_league=False, require_evidence=True,
                            cross_book_evidence=False, drift_evidence=False)
    print(f"bettable={b.get('bettable')} n_bet={b.get('n_bet')} n_observe={b.get('n_observe')}")
    for v in b.get("verdicts", []):
        print(f"  {v['score']:>4} @ {v['odds']:>6}  div={v['divergence']:+.3f} → {v['verdict']}")

    print("\n══════════ 场景C: 同场标 obscure_league=True → 强制只观察(绝不自动BET) ══════════")
    c = decide_cs_from_json(SAMPLE, cur_h=0, cur_a=0, minutes_played=0.0,
                            obscure_league=True, require_evidence=False)
    print(f"obscure={c.get('obscure_league')} bettable={c.get('bettable')} n_bet={c.get('n_bet')}")
    for v in c.get("verdicts", []):
        print(f"  {v['score']:>4} @ {v['odds']:>6}  div={v['divergence']:+.3f} → {v['verdict']}")
