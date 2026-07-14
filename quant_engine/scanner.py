# -*- coding: utf-8 -*-
"""全市场价值扫描器 — 单场比赛 → 所有可投注选项的价值排序.

这是系统的大脑. 对一场比赛的每个市场 (1X2/波胆/总进球/让球/大小球):
  1. 用 OIP 模型算出该选项的「真实概率」
  2. 用 compute_value_layer / compute_submarket_value 算 edge/EV/凯利
  3. 按价值排序, 标注 BET / EVAL / SCAN

诚实铁律 (沿用项目既有哲学):
  - 多庄 (≥2): 可证伪 → edge 可信 → 触发 BET
  - 单庄: 不可证伪 → 只标 EVAL (展示价值但不伪称结论)
  - 无挂盘: 标 SCAN (纯模型概率, 不算 edge)

绝不重造: 全部数学委托 SSoT.
"""
from __future__ import annotations
import math
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

from .market_feeder import MatchMarket

# ── SSoT imports (纯函数, 只读复用) ──
from pipeline.deep_report import (
    compute_value_layer, compute_submarket_value, consensus_probs,
    kelly_fraction,
)
from pipeline.score_model import predict_score


# ── 数据结构 ──────────────────────────────────────────────────

@dataclass
class OptionValuation:
    """单个投注选项的估值."""
    market: str             # 市场名 (1X2/波胆/总进球/让球/大小球)
    selection: str          # 选项名 (H/D/A 或 "1-0" 或 "2球" 等)
    odds: float             # 赔率 (0 表示无挂盘)
    model_prob: float       # 模型概率
    market_prob: float      # 市场隐含概率 (0 表示无挂盘)
    edge_pct: float         # edge %
    ev_pct: float           # 期望价值 %
    kelly_half: float       # 半凯利比例
    decision: str = "SCAN"  # BET / EVAL / SCAN / PASS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "market": self.market, "selection": self.selection,
            "odds": round(self.odds, 3) if self.odds else None,
            "model_prob": round(self.model_prob, 4),
            "market_prob": round(self.market_prob, 4) if self.market_prob else None,
            "edge_pct": round(self.edge_pct, 2),
            "ev_pct": round(self.ev_pct, 2),
            "kelly_half": round(self.kelly_half, 4),
            "decision": self.decision,
        }


@dataclass
class ScanResult:
    """一场比赛的完整扫描结果."""
    mid: str
    home: str
    away: str
    league: str
    is_multi_book: bool
    options: List[OptionValuation] = field(default_factory=list)
    # OIP 模型摘要
    expected_goals: Optional[Dict[str, float]] = None  # {lh, la, total}
    top_scores: Optional[List[Dict[str, Any]]] = None
    # 价值排序后的 BET 候选
    bet_candidates: List[OptionValuation] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mid": self.mid, "home": self.home, "away": self.away,
            "league": self.league, "is_multi_book": self.is_multi_book,
            "expected_goals": self.expected_goals,
            "top_scores": self.top_scores,
            "options": [o.to_dict() for o in self.options],
            "bet_candidates": [o.to_dict() for o in self.bet_candidates],
            "n_options": len(self.options),
            "n_bets": len(self.bet_candidates),
            "best_option": self.bet_candidates[0].to_dict() if self.bet_candidates else None,
        }


# ── 扫描主函数 ────────────────────────────────────────────────

def scan(m: MatchMarket, bankroll: float = 3000.0, frac_kelly: float = 0.5) -> ScanResult:
    """对一场比赛做全市场价值扫描."""
    result = ScanResult(
        mid=m.mid, home=m.home, away=m.away, league=m.league,
        is_multi_book=m.is_multi_book,
    )
    if not any(x > 0 for x in m.best_h2h):
        return result

    oh, od, oa = m.best_h2h

    # ① OIP 波胆模型 (全市场概率的基础)
    try:
        oip = predict_score(m.home, m.away, oh, od, oa, max_goal=8)
        M = oip["matrix"]
        lh, la = float(oip["lh"]), float(oip["la"])
        result.expected_goals = {"lh": lh, "la": la, "total": round(lh + la, 3)}
        result.top_scores = [
            {"score": f"{s[0]}-{s[1]}", "prob": s[2]} for s in oip["top_scores"]
        ]
        model_1x2 = [float(oip["p_h"]), float(oip["p_d"]), float(oip["p_a"])]
    except Exception:
        # OIP 失败则退化为市场隐含
        M = None
        model_1x2 = consensus_probs([list(m.best_h2h)])

    multi = m.is_multi_book
    # ② 标准 1X2 价值层
    _scan_1x2(result, m, model_1x2, multi, bankroll, frac_kelly)

    # ③ 波胆 (有挂盘才算 edge, 否则 SCAN)
    if m.score_odds:
        _scan_correct_score(result, m, M, multi, bankroll, frac_kelly)
    elif M is not None:
        # 无挂盘: 只输出 top 比分概率 (SCAN, 供展示)
        _scan_score_scan_only(result, M)

    # ④ 总进球 (有挂盘才算 edge)
    if m.total_goals_odds and M is not None:
        _scan_total_goals(result, m, M, multi, bankroll, frac_kelly)

    # ⑤ 让球 (有挂盘才算)
    if m.handicap_odds and M is not None:
        _scan_handicap(result, m, M, multi, bankroll, frac_kelly)

    # ⑥ 大小球 (有挂盘才算)
    if m.ou_odds and M is not None:
        _scan_ou(result, m, M, multi, bankroll, frac_kelly)

    # 排序: BET 候选按 edge 降序
    result.bet_candidates = sorted(
        [o for o in result.options if o.decision == "BET" and o.odds > 0],
        key=lambda x: x.edge_pct, reverse=True,
    )
    return result


# ── 各市场扫描实现 ────────────────────────────────────────────

def _decide(multi: bool, ev: float, kelly: float) -> str:
    """决策逻辑: 多庄+正EV+正凯利→BET; 单庄正EV→EVAL; 否则PASS."""
    if ev <= 0 or kelly <= 0:
        return "PASS"
    return "BET" if multi else "EVAL"


def _scan_1x2(result, m, model_1x2, multi, bankroll, frac_kelly):
    """标准 1X2 价值层."""
    if m.is_multi_book:
        # 多庄: 共识概率 + 跨庄最优价
        books_list = [[b.h, b.d, b.a] for b in m.books]
        cons = consensus_probs(books_list)
        vl = compute_value_layer(odds=m.best_h2h, model_probs=cons,
                                 bankroll=bankroll, frac_kelly=frac_kelly, gate=True)
        for r in vl["rows"]:
            dec = vl["decision"] if (r["outcome"] == vl.get("best_direction") and r["ev"] > 0) else \
                  _decide(multi, r["ev"], r["kelly_half"])
            result.options.append(OptionValuation(
                market="标准胜平负", selection={"H": "主胜", "D": "平局", "A": "客胜"}[r["outcome"]],
                odds=r["odds"], model_prob=r["model_prob"], market_prob=r["market_prob"],
                edge_pct=r["edge_pct"], ev_pct=r["ev_pct"], kelly_half=r["kelly_half"],
                decision=dec,
            ))
    else:
        # 单庄: OIP 概率 vs 市场隐含 (EVAL, 不伪称)
        inv_sum = 1.0 / m.best_h2h[0] + 1.0 / m.best_h2h[1] + 1.0 / m.best_h2h[2]
        mkt = [1.0 / o / inv_sum for o in m.best_h2h]
        for i, sel in enumerate(["主胜", "平局", "客胜"]):
            o = m.best_h2h[i]
            p = model_1x2[i]
            edge = p - mkt[i]
            ev = p * o - 1.0
            k = kelly_fraction(p, o)
            result.options.append(OptionValuation(
                market="标准胜平负", selection=sel, odds=o,
                model_prob=p, market_prob=mkt[i], edge_pct=edge * 100,
                ev_pct=ev * 100, kelly_half=k * frac_kelly,
                decision=_decide(multi, ev, k),
            ))


def _scan_correct_score(result, m, M, multi, bankroll, frac_kelly):
    """波胆价值层 (有挂盘)."""
    if M is None:
        return
    import numpy as np
    M = np.asarray(M, dtype=float)
    n = M.shape[0]
    for score_str, odds in m.score_odds.items():
        try:
            sh, sa = score_str.split("-")
            sh, sa = int(sh), int(sa)
        except Exception:
            continue
        if sh > n - 1 or sa > n - 1:
            continue
        p = float(M[sh, sa])
        mkt = 1.0 / odds if odds > 0 else 0.0
        edge = p - mkt
        ev = p * odds - 1.0
        k = kelly_fraction(p, odds)
        result.options.append(OptionValuation(
            market="波胆", selection=score_str, odds=odds,
            model_prob=p, market_prob=mkt, edge_pct=edge * 100,
            ev_pct=ev * 100, kelly_half=k * frac_kelly,
            decision=_decide(multi, ev, k),
        ))


def _scan_score_scan_only(result, M):
    """无挂盘: 输出 top5 比分概率 (SCAN, 纯展示)."""
    if M is None or not result.top_scores:
        return
    for ts in result.top_scores:
        sh, sa = ts["score"].split("-")
        result.options.append(OptionValuation(
            market="波胆(预测)", selection=ts["score"], odds=0.0,
            model_prob=ts["prob"], market_prob=0.0,
            edge_pct=0.0, ev_pct=0.0, kelly_half=0.0, decision="SCAN",
        ))


def _scan_total_goals(result, m, M, multi, bankroll, frac_kelly):
    """总进球价值层 (有挂盘)."""
    import numpy as np
    M = np.asarray(M, dtype=float)
    n = M.shape[0]
    # 从矩阵聚合 P(总进球=k)
    goal_probs = {}
    for total in range(0, n * 2):
        p = float(sum(M[i, j] for i in range(n) for j in range(n) if i + j == total))
        goal_probs[total] = p
    # 7+球合并
    p_7plus = sum(v for k, v in goal_probs.items() if k >= 7)
    for label, odds in m.total_goals_odds.items():
        try:
            goals = int(label.replace("+", "").replace("球", "").strip())
        except Exception:
            continue
        if label.startswith("7") or "+" in label:
            p = p_7plus
        elif goals in goal_probs:
            p = goal_probs[goals]
        else:
            continue
        mkt = 1.0 / odds if odds > 0 else 0.0
        edge = p - mkt
        ev = p * odds - 1.0
        k = kelly_fraction(p, odds)
        result.options.append(OptionValuation(
            market="总进球", selection=f"{label}球", odds=odds,
            model_prob=p, market_prob=mkt, edge_pct=edge * 100,
            ev_pct=ev * 100, kelly_half=k * frac_kelly,
            decision=_decide(multi, ev, k),
        ))


def _scan_handicap(result, m, M, multi, bankroll, frac_kelly):
    """让球价值层 (有挂盘, 用矩阵算覆盖概率)."""
    import numpy as np
    M = np.asarray(M, dtype=float)
    n = M.shape[0]
    hc = m.handicap_odds
    line = float(hc.get("line", 0))  # 负=主让, 正=主受让
    # 主让 line 球: 主队净胜 + line > 0 → 主胜盘; ==0 走水; <0 客胜盘
    p_home = p_draw = p_away = 0.0
    for i in range(n):
        for j in range(n):
            margin = (i - j) + line  # 主让后净胜
            if margin > 0:
                p_home += float(M[i, j])
            elif margin == 0:
                p_draw += float(M[i, j])
            else:
                p_away += float(M[i, j])
    # 走水算半
    p_home += p_draw * 0.5
    p_away += p_draw * 0.5
    for sel, odds, p in [("主胜(让)", hc.get("home", 0), p_home),
                         ("平(让)", hc.get("draw", 0), p_draw),
                         ("客胜(让)", hc.get("away", 0), p_away)]:
        if not odds or odds <= 0:
            continue
        mkt = 1.0 / odds
        edge = p - mkt
        ev = p * odds - 1.0
        k = kelly_fraction(p, odds)
        result.options.append(OptionValuation(
            market=f"让球({int(line)})", selection=sel, odds=float(odds),
            model_prob=p, market_prob=mkt, edge_pct=edge * 100,
            ev_pct=ev * 100, kelly_half=k * frac_kelly,
            decision=_decide(multi, ev, k),
        ))


def _scan_ou(result, m, M, multi, bankroll, frac_kelly):
    """大小球价值层 (有挂盘)."""
    import numpy as np
    M = np.asarray(M, dtype=float)
    n = M.shape[0]
    ou = m.ou_odds
    line = float(ou.get("line", 2.5))
    p_over = p_under = 0.0
    for i in range(n):
        for j in range(n):
            total = i + j
            if total > line:
                p_over += float(M[i, j])
            elif total < line:
                p_under += float(M[i, j])
    for sel, odds, p in [("大", ou.get("over", 0), p_over),
                         ("小", ou.get("under", 0), p_under)]:
        if not odds or odds <= 0:
            continue
        mkt = 1.0 / odds
        edge = p - mkt
        ev = p * odds - 1.0
        k = kelly_fraction(p, odds)
        result.options.append(OptionValuation(
            market=f"大小球({line})", selection=sel, odds=float(odds),
            model_prob=p, market_prob=mkt, edge_pct=edge * 100,
            ev_pct=ev * 100, kelly_half=k * frac_kelly,
            decision=_decide(multi, ev, k),
        ))
