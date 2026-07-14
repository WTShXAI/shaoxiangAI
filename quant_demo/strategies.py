# -*- coding: utf-8 -*-
"""策略层 (演示): 多策略注册 + 统一信号接口.

复用 SSoT 纯函数:
  - pipeline.deep_report.compute_value_layer  (edge/EV/凯利/决策)
  - pipeline.reverse_odds_engine              (跨庄分歧检测 / soft-line)
绝不 import pipeline.engine / wc_engine / league_engine.

每个策略 = 同一套价值层数学 + 不同的「触发过滤器」, 对应你系统的真实打法:
  - ValueLayerDivergence: 分歧闸门 + 价值层 (今天修好的 +83.76% 路径)
  - SharpFade:            soft-line 热门淡化(0.41)
  - DrawInsurance:        双庄共识平局保险
"""
from typing import List, Dict, Any, Optional
from .types import SyntheticMatch, StrategySignal, StrategyMeta


# ── 单一事实源 import (纯函数, 零 DB/模型耦合) ──
from pipeline.deep_report import compute_value_layer
from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput

_ENG = ReverseOddsEngine()
_IDX = {"H": 0, "D": 1, "A": 2}


def _build_consensus_probs(m: SyntheticMatch) -> List[float]:
    return m.consensus_prob


def _detect_disagreement(m: SyntheticMatch) -> bool:
    bA = m.books[0]; bB = m.books[1]
    a = OddsInput(open_h=bA["h"], open_d=bA["d"], open_a=bA["a"],
                  close_h=bA["h"], close_d=bA["d"], close_a=bA["a"])
    b = OddsInput(open_h=bB["h"], open_d=bB["d"], open_a=bB["a"],
                  close_h=bB["h"], close_d=bB["d"], close_a=bB["a"])
    res = _ENG.analyze_multi([a, b])
    return bool(res.disagreement_detected)


def _value_layer(m: SyntheticMatch) -> Dict[str, Any]:
    cons = _build_consensus_probs(m)
    vl = compute_value_layer(odds=m.best_odds, model_probs=cons,
                              bankroll=3000.0, frac_kelly=0.5)
    return vl


class BaseStrategy:
    id: str = ""
    name: str = ""
    desc: str = ""

    def signal(self, m: SyntheticMatch) -> StrategySignal:
        raise NotImplementedError


class ValueLayerDivergenceStrategy(BaseStrategy):
    id = "vl_divergence"
    name = "分歧闸门·价值层"
    desc = "跨庄结构性分歧 + 价值层 edge>0 才建仓 (核心 edge 过滤器)"

    def signal(self, m: SyntheticMatch) -> StrategySignal:
        vl = _value_layer(m)
        dis = _detect_disagreement(m)
        if vl["decision"] == "BET" and dis:
            d = vl["best_direction"]
            row = next((r for r in vl["rows"] if r["outcome"] == d), None)
            return StrategySignal(
                strategy_id=self.id, strategy_name=self.name,
                decision="BET", direction=d,
                best_odds=m.best_odds[_IDX[d]],
                edge_pct=row["edge_pct"] if row else None,
                ev_pct=row["ev_pct"] if row else None,
                kelly_half=row["kelly_half"] if row else None,
                note="结构性分歧 + 价值层 edge>0",
            )
        return StrategySignal(self.id, self.name, "PASS",
                              note=f"无分歧或edge不足 (dis={dis}, vl={vl['decision']})")


class SharpFadeStrategy(BaseStrategy):
    id = "sharp_fade"
    name = "Soft-line 热门淡化"
    desc = "跨庄共识热门被淡化(0.41)时, 反向下注被低估方"

    def signal(self, m: SyntheticMatch) -> StrategySignal:
        vl = _value_layer(m)
        # 演示触发: 含 sharp_fade 场景 且 价值层给出非热门方 BET
        if "sharp_fade" in m.scenarios and vl["decision"] == "BET":
            d = vl["best_direction"]
            # 淡化热门=不押 H, 押被低估的 D/A
            if d != "H":
                row = next((r for r in vl["rows"] if r["outcome"] == d), None)
                return StrategySignal(
                    self.id, self.name, "BET", direction=d,
                    best_odds=m.best_odds[_IDX[d]],
                    edge_pct=row["edge_pct"] if row else None,
                    ev_pct=row["ev_pct"] if row else None,
                    kelly_half=row["kelly_half"] if row else None,
                    note="热门淡化, 反向下注被低估方",
                )
        return StrategySignal(self.id, self.name, "PASS",
                              note="无热门淡化信号")


class DrawInsuranceStrategy(BaseStrategy):
    id = "draw_insurance"
    name = "双庄共识平局保险"
    desc = "P平>=26% 时防平 (共识平局 edge>0 才建仓)"

    def signal(self, m: SyntheticMatch) -> StrategySignal:
        p_draw = m.consensus_prob[1]
        vl = _value_layer(m)
        draw_row = next((r for r in vl["rows"] if r["outcome"] == "D"), None)
        if p_draw >= 0.26 and draw_row and draw_row["ev"] > 0:
            return StrategySignal(
                self.id, self.name, "BET", direction="D",
                best_odds=m.best_odds[1],
                edge_pct=draw_row["edge_pct"], ev_pct=draw_row["ev_pct"],
                kelly_half=draw_row["kelly_half"],
                note=f"P平={p_draw*100:.1f}% 防平建仓",
            )
        return StrategySignal(self.id, self.name, "PASS",
                              note=f"P平={p_draw*100:.1f}% 未达防平阈值")


# 注册表
REGISTRY: Dict[str, BaseStrategy] = {
    s.id: s for s in [
        ValueLayerDivergenceStrategy(),
        SharpFadeStrategy(),
        DrawInsuranceStrategy(),
    ]
}

META: List[StrategyMeta] = [
    StrategyMeta(s.id, s.name, s.desc) for s in REGISTRY.values()
]


def generate_signals(m: SyntheticMatch, enabled: List[str]) -> List[StrategySignal]:
    """对一场比赛跑所有启用策略, 返回信号列表."""
    out = []
    for sid in enabled:
        st = REGISTRY.get(sid)
        if st:
            out.append(st.signal(m))
    return out


def edge_of(m: SyntheticMatch) -> List[float]:
    """返回 [H,D,A] edge(%) — 供合成赛果按 edge 调整真实概率."""
    vl = _value_layer(m)
    out = [0.0, 0.0, 0.0]
    for r in vl.get("rows", []):
        i = _IDX.get(r["outcome"])
        if i is not None:
            out[i] = r.get("edge_pct", 0.0) or 0.0
    return out
