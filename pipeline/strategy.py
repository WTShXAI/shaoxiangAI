# -*- coding: utf-8 -*-
"""pipeline/strategy.py
================================
策略层 + 组合层 · 单一事实源 (SSoT)  —  P0 #19 「底座」

系统闭环位置 (ARCHITECTURE.md §3)：
    价值层 compute_value_layer  →  【策略层(多策略注册) → 组合层聚合】  →  bet_core 注码  →  执行/绩效

本模块补足「价值层 → bet_core」之间缺失的策略/组合层地基：
  - 数据契约: ValueSignal(单场候选) / BetIntent(最终下单) / Constraints(组合约束) / BetPlan(最终计划)
  - 策略注册表: BaseStrategy + REGISTRY (本层生产 SSoT; bookmaker_sim.strategy_registry 属模拟器域)
  - 组合聚合器: build_portfolio() — 把多场候选按组合约束(总暴露上限/注数/edge阈值)聚合成 BetPlan
  - 基准策略: KellyAggregateStrategy(组合层) + ValueLayerDivergenceStrategy(per-match 信号)

铁律 (第一性原理)：
  - 注码/凯利唯一事实源 = scripts.bet_core。最终注码必须过 safe_stake (10%封顶+分歧闸门+PROD护栏)。
  - 价值层唯一事实源 = pipeline.compute_value_layer。本层只消费其输出, 不重造 edge/EV/决策。
  - 严禁直接使用 compute_value_layer 返回的 stake_unit (文档明示「仅供展示」); 真实注码一律 safe_stake 重算。
  - 只收 decision=="BET" 的候选入组合 (可证伪原则)。
  - 仅依赖标准库 + bet_core + compute_value_layer; 无 numpy / 无 torch / 无重依赖。

依赖：scripts.bet_core (SSoT), pipeline.compute_value_layer (SSoT)。二者均仅标准库+PyYAML。
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── 注码/凯利 SSoT (scripts.bet_core) ──
# 若不可用直接报错, 不静默用过期副本 (与 compute_value_layer 一致)
try:
    from scripts.bet_core import (
        safe_stake as _safe_stake,
        kelly_fraction as _kelly_fraction,
        FRAC_KELLY as _FRAC_KELLY,
        MAX_STAKE_FRAC as _MAX_STAKE_FRAC,
    )
    _HAS_BET_CORE = True
except Exception as _e:  # pragma: no cover
    raise ImportError(
        "pipeline.strategy 依赖 scripts.bet_core (注码/凯利唯一事实源); "
        "请确认 scripts/ 在 sys.path 且已安装 PyYAML。"
    ) from _e


# ── 价值层 SSoT (pipeline.compute_value_layer) ──
try:
    from pipeline.compute_value_layer import compute_value_layer as _compute_value_layer
    _HAS_VALUE_LAYER = True
except Exception as _e:  # pragma: no cover
    _compute_value_layer = None
    _HAS_VALUE_LAYER = False


# ════════════════════════════════════════════════════════════════
# 1. 数据契约
# ════════════════════════════════════════════════════════════════

@dataclass
class ValueSignal:
    """单场比赛的候选下注信号 (来自价值层 / 某策略)。

    只有 decision=="BET" 且 odds>1 的信号才会进入组合层聚合。
    model_prob / odds 是组合层算最终注码 (过 safe_stake) 的必需输入。
    """
    mid: str = ""                 # match id
    home: str = ""
    away: str = ""
    market: str = "1X2"           # 1X2 / AH / OU / CS
    selection: str = ""           # H / D / A 或子市场标签 (如 "Over 2.5")
    odds: float = 0.0
    model_prob: float = 0.0
    edge_pct: float = 0.0
    ev_pct: float = 0.0
    kelly_half: float = 0.0
    decision: str = "PASS"        # BET / PASS
    strategy_id: str = "value_layer"
    note: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ValueSignal":
        """由字典安全构造 (前端/API 传入)。未知字段忽略。"""
        return cls(
            mid=str(d.get("mid", "")),
            home=str(d.get("home", "")),
            away=str(d.get("away", "")),
            market=str(d.get("market", "1X2")),
            selection=str(d.get("selection", "")),
            odds=float(d.get("odds", 0.0) or 0.0),
            model_prob=float(d.get("model_prob", 0.0) or 0.0),
            edge_pct=float(d.get("edge_pct", 0.0) or 0.0),
            ev_pct=float(d.get("ev_pct", 0.0) or 0.0),
            kelly_half=float(d.get("kelly_half", 0.0) or 0.0),
            decision=str(d.get("decision", "PASS")),
            strategy_id=str(d.get("strategy_id", "value_layer")),
            note=str(d.get("note", "")),
        )


@dataclass
class BetIntent:
    """组合层最终下单意图 (注码已过 bet_core.safe_stake 封顶)。"""
    mid: str
    home: str
    away: str
    market: str
    selection: str
    odds: float
    model_prob: float
    edge_pct: float
    stake: float                 # 最终注码 (来自 safe_stake)
    kelly_frac: float            # 实际生效的凯利占比 (已含组合缩放)
    strategy_id: str
    note: str = ""


@dataclass
class Constraints:
    """组合层约束 (P0 硬约束集)。"""
    max_exposure: float = 0.30       # 组合总暴露上限 (占本金比例)
    max_bets: int = 10               # 同时最大注数
    min_edge_pct: float = 0.0        # 最小 edge 阈值 (低于丢弃)
    frac_kelly: float = _FRAC_KELLY  # 凯利比例 (默认半凯利)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Constraints":
        if not d:
            return cls()
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class BetPlan:
    """组合层最终投注计划。"""
    intents: List[BetIntent] = field(default_factory=list)
    total_stake: float = 0.0
    total_exposure_pct: float = 0.0
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    constraints: Constraints = field(default_factory=Constraints)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intents": [
                {
                    "mid": b.mid, "home": b.home, "away": b.away,
                    "market": b.market, "selection": b.selection, "odds": b.odds,
                    "model_prob": round(b.model_prob, 4), "edge_pct": b.edge_pct,
                    "stake": round(b.stake, 2), "kelly_frac": round(b.kelly_frac, 4),
                    "strategy_id": b.strategy_id, "note": b.note,
                }
                for b in self.intents
            ],
            "total_stake": round(self.total_stake, 2),
            "total_exposure_pct": round(self.total_exposure_pct, 4),
            "rejected": self.rejected,
            "constraints": {
                "max_exposure": self.constraints.max_exposure,
                "max_bets": self.constraints.max_bets,
                "min_edge_pct": self.constraints.min_edge_pct,
                "frac_kelly": self.constraints.frac_kelly,
            },
            "bet_count": len(self.intents),
        }


# ════════════════════════════════════════════════════════════════
# 2. 策略注册表 (本层生产 SSoT; 轻量, 纯标准库, 导入安全)
# ════════════════════════════════════════════════════════════════

class BaseStrategy:
    """策略基类: 把一场比赛转成 ValueSignal 列表 (per-match 信号生产者)。"""
    id: str = ""
    name: str = ""
    desc: str = ""

    def signals(self, match: Dict[str, Any]) -> List[ValueSignal]:
        raise NotImplementedError


_REGISTRY: Dict[str, BaseStrategy] = {}


def register_strategy(strategy: BaseStrategy) -> BaseStrategy:
    """注册一个 per-match 策略实例 (幂等, 同名覆盖并告警)。"""
    sid = strategy.id or strategy.__class__.__name__
    if sid in _REGISTRY:
        import logging
        logging.getLogger("strategy").warning(f"策略已注册, 覆盖: {sid}")
    _REGISTRY[sid] = strategy
    return strategy


def get_registry() -> Dict[str, BaseStrategy]:
    """返回已注册策略字典 (callable 风格, 与 bookmaker_sim 接口对齐)。"""
    return _REGISTRY


def list_strategies() -> List[Dict[str, str]]:
    return [{"id": s.id, "name": s.name, "desc": s.desc} for s in _REGISTRY.values()]


# ════════════════════════════════════════════════════════════════
# 3. 组合聚合器 (P0 核心: B 总暴露上限 + H 基准 Kelly)
# ════════════════════════════════════════════════════════════════

def build_portfolio(
    signals: List[ValueSignal],
    bankroll: float = 3000.0,
    constraints: Optional[Constraints] = None,
    gate: bool = True,
    notify: bool = False,
) -> BetPlan:
    """把多场候选聚合成受约束的最终投注计划 (BetPlan)。

    流程 (全部最终注码过 bet_core.safe_stake, 守住10%封顶+闸门+PROD护栏)：
      1. 只收 decision=="BET" 且 odds>1
      2. edge 阈值过滤 (min_edge_pct)
      3. 按 edge 降序取前 max_bets
      4. 每注期望 frac = kelly_fraction × frac_kelly (聚合凯利)
      5. 总暴露硬约束: 若 Σfrac > max_exposure, 等比缩放
      6. 每注最终注码 = safe_stake(p, o, bankroll, frac_kelly=target_frac/k)
          (safe_stake 内部再强制单注 ≤ MAX_STAKE_FRAC)

    Args:
        signals:    候选信号列表
        bankroll:   本金基准
        constraints:组合约束 (None=默认 Constraints())
        gate:       分歧闸门 (False → 所有注码为0, 即全 PASS)
        notify:     是否在有 BET 产出时触发 Telegram 推送 (fire-and-forget, 非阻塞)
    Returns:
        BetPlan
    """
    c = constraints or Constraints()
    rejected: List[Dict[str, Any]] = []

    # 1. 只收 BET
    bets = [s for s in signals if s.decision == "BET" and s.odds > 1.0]
    for s in signals:
        if s.decision != "BET" or s.odds <= 1.0:
            rejected.append({"mid": s.mid, "reason": "not_bet", "decision": s.decision})

    # 2. edge 阈值
    passed = []
    for s in bets:
        if s.edge_pct >= c.min_edge_pct:
            passed.append(s)
        else:
            rejected.append({"mid": s.mid, "reason": "edge_below_threshold", "edge_pct": s.edge_pct})

    # 3. 按 edge 降序, 截断 max_bets
    passed.sort(key=lambda s: s.edge_pct, reverse=True)
    if len(passed) > c.max_bets:
        for s in passed[c.max_bets:]:
            rejected.append({"mid": s.mid, "reason": "max_bets_exceeded"})
        passed = passed[: c.max_bets]

    # 4. 聚合凯利期望占比
    desired: List[tuple] = []
    for s in passed:
        k = _kelly_fraction(s.model_prob, s.odds)
        if k <= 0:
            rejected.append({"mid": s.mid, "reason": "kelly_nonpositive"})
            continue
        desired.append((s, k * c.frac_kelly))

    # 5. 总暴露硬约束: 等比缩放
    sum_frac = sum(f for _, f in desired)
    scale = (c.max_exposure / sum_frac) if sum_frac > c.max_exposure > 0 else 1.0

    # 6. 最终注码一律过 safe_stake
    intents: List[BetIntent] = []
    total_stake = 0.0
    for s, desired_frac in desired:
        target_frac = desired_frac * scale
        k = _kelly_fraction(s.model_prob, s.odds)
        # safe_stake: stake = frac_kelly × k × bankroll; 故 frac_kelly_in = target_frac / k
        fk = (target_frac / k) if k > 0 else 0.0
        stake, _ = _safe_stake(
            s.model_prob, s.odds, bankroll,
            frac_kelly=fk, gate=gate, source="strategy_layer",
        )
        if stake <= 0:
            rejected.append({"mid": s.mid, "reason": "safe_stake_zero", "gate": gate})
            continue
        intents.append(BetIntent(
            mid=s.mid, home=s.home, away=s.away, market=s.market,
            selection=s.selection, odds=s.odds, model_prob=s.model_prob,
            edge_pct=s.edge_pct, stake=round(stake, 2),
            kelly_frac=round(target_frac, 4), strategy_id=s.strategy_id, note=s.note,
        ))
        total_stake += stake

    total_exposure_pct = (total_stake / bankroll) if bankroll > 0 else 0.0
    plan = BetPlan(
        intents=intents,
        total_stake=round(total_stake, 2),
        total_exposure_pct=round(total_exposure_pct, 4),
        rejected=rejected,
        constraints=c,
    )

    # ── Telegram 推送触发点 (fire-and-forget, 非阻塞) ──
    if notify and intents:
        _maybe_notify_telegram(plan)

    return plan


def _maybe_notify_telegram(plan: BetPlan) -> None:
    """将 BetPlan 的 intents 推送到 Telegram (fire-and-forget)。

    此函数在独立线程中执行, 绝不阻塞主分析流程。
    异常完全内部消化, 不向上抛。
    """
    import threading

    def _push():
        try:
            from pipeline.notifiers import intents_to_signals, get_notifier
            notifier = get_notifier()
            signals = intents_to_signals(plan.intents)
            notifier.notify_signals_sync(signals)
        except Exception:
            # 静默吞掉所有异常: Telegram 推送失败不能影响主流程
            pass

    t = threading.Thread(target=_push, daemon=True, name="tg-notify-build-portfolio")
    t.start()


# ════════════════════════════════════════════════════════════════
# 4. 基准策略
# ════════════════════════════════════════════════════════════════

class KellyAggregateStrategy:
    """组合层基准策略 (H): 多场候选 → 聚合凯利 BetPlan。

    组合层不关心每个候选怎么来的 (价值层/某策略), 只负责把候选按组合约束聚合。
    """
    id = "kelly_aggregate"
    name = "聚合凯利·组合层"
    desc = "多场候选按聚合凯利分配, 总暴露钉死上限, 每注过 bet_core 封顶"

    def build(
        self,
        signals: List[ValueSignal],
        bankroll: float = 3000.0,
        constraints: Optional[Constraints] = None,
        gate: bool = True,
    ) -> BetPlan:
        return build_portfolio(signals, bankroll, constraints, gate)


@register_strategy
class ValueLayerDivergenceStrategy(BaseStrategy):
    """per-match 基准策略 (分歧闸门·价值层): 价值层 BET + 跨庄分歧 → ValueSignal。

    演示原型移植自 quant_demo.strategies.ValueLayerDivergenceStrategy, 生产化:
      - 不再依赖 SyntheticMatch, 直接吃 {odds, model_probs, mid, home, away}
      - 走价值层 SSoT compute_value_layer
    注意: 跨庄分歧检测 (ReverseOddsEngine) 属破解层, 本策略默认信任价值层已含分歧闸门
    (compute_value_layer 的 gate 参数)。如需显式分歧过滤, 由上游传入 decision 即可。
    """
    id = "vl_divergence"
    name = "分歧闸门·价值层"
    desc = "价值层 edge>0 且决策为 BET 才建仓 (核心 edge 过滤器)"

    def signals(self, match: Dict[str, Any]) -> List[ValueSignal]:
        if not _HAS_VALUE_LAYER:
            return []
        odds = match.get("odds")
        model_probs = match.get("model_probs")
        if not odds or not model_probs:
            return []
        vl = _compute_value_layer(
            odds=odds, model_probs=model_probs,
            bankroll=float(match.get("bankroll", 3000.0)),
            frac_kelly=match.get("frac_kelly", _FRAC_KELLY),
            gate=match.get("gate", True),
        )
        if vl.get("decision") != "BET":
            return []
        d = vl.get("best_direction")
        row = next((r for r in vl.get("rows", []) if r.get("outcome") == d), None)
        if not row:
            return []
        return [ValueSignal(
            mid=str(match.get("mid", "")),
            home=str(match.get("home", "")),
            away=str(match.get("away", "")),
            market="1X2",
            selection=d,
            odds=float(row["odds"]),
            model_prob=float(row["model_prob"]),
            edge_pct=float(row["edge_pct"]),
            ev_pct=float(row["ev_pct"]),
            kelly_half=float(row["kelly_half"]),
            decision="BET",
            strategy_id=self.id,
            note="价值层+分歧闸门",
        )]


__all__ = [
    "ValueSignal", "BetIntent", "Constraints", "BetPlan",
    "BaseStrategy", "register_strategy", "get_registry", "list_strategies",
    "build_portfolio", "KellyAggregateStrategy", "ValueLayerDivergenceStrategy",
]
