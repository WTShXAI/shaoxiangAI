#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bet_core: 统一投注核心 (P0-1 / P0-2 / P0-3 / live_pilot 单一事实源).

把分散在 p0_2.decide / p0_3.decide_dir 的重复下注逻辑收敛到一处, 杜绝公式漂移:
  - kelly_fraction 封装 (规范满凯利分数 (p*o-1)/(o-1); 已修正 P0-3 误用 0.5*(p*o-1) 当分数的坑)
  - 规范 1/4 凯利 (FRAC_KELLY=0.25) + 单注封顶 10% (MAX_STAKE_FRAC; P0-2 修复的 kelly>1 全押 bug)
  - decide_argmax / decide_direction 两个方向入口, 底层同实现
  - P0-1 价值闸门: PROD 环境强制 value_layer_approved, 裸调 flat argmax → NO-BET
  - P0-2 回撤预算: 动态 Kelly 缩放 (calc_drawdown_scale) + 月度止盈止损 (check_monthly_limit)

所有回测/守护脚本均 import 本模块; 任何公式改动只在此一处, 由 tests/test_bet_core.py 守护.
"""
import os
import sys
import logging
import json
import yaml
from typing import Dict, Any, Optional, Tuple, List

# 仓库根 (兼容本地 Windows 与 Linux CI runner), 不写死绝对路径
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── 审计日志 ──
_audit_logger = logging.getLogger("bet_core.audit")
_audit_logger.setLevel(logging.INFO)
if not _audit_logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
    _audit_logger.addHandler(_h)


def _load_betting_config() -> Dict[str, Any]:
    """从 config/expert_registry.yaml 加载投注参数; 失败则回退硬编码默认值."""
    config_path = os.path.join(_ROOT, "config", "expert_registry.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("betting", {})
    except Exception:
        return {}

_BC = _load_betting_config()

BANKROLL = float(_BC.get("bankroll", 3000.0))
FRAC_KELLY = float(_BC.get("frac_kelly", 0.25))
MAX_STAKE_FRAC = float(_BC.get("max_stake_frac", 0.10))
MIN_DISAGREEMENT_BETS = int(_BC.get("min_disagreement_bets", 300))
_ENV = str(_BC.get("env", "DEV")).upper()

# ── P0a 注码风控闸门 (2026-08-01, 针对 ROI=-30% 根因: 74%本金重注低赔热门全输) ──
# 低赔热门限注: odds<HOT_ODDS_MAX 视为市场强热门, 单注从 MAX_STAKE_FRAC 压到 HOT_FRAC_CAP。
# 这是 ROI=-30%(14单) 的核心止血: 此前 decide_argmax 押热门时 10% 封顶照放满所致。
HOT_ODDS_MAX = float(_BC.get("hot_odds_max", 1.5))
HOT_FRAC_CAP = float(_BC.get("hot_frac_cap", 0.03))
HOT_EDGE_MIN_PP = float(_BC.get("hot_edge_min_pp", 3.0))  # P0-3: 热门主动拒止门槛
# 跨庄分歧放注门槛 (P1 接入跨庄后启用; 默认0=不启用): 仅跨庄 spread≥min_spread_pp(pp) 才放注
MIN_SPREAD_PP = float(_BC.get("min_spread_pp", 0.0))
# 连败降档 (默认0=不启用): 连输 loss_cooldown_after 场后 frac *= LOSS_FRAC_SCALE
LOSS_COOLDOWN_AFTER = int(_BC.get("loss_cooldown_after", 0))
LOSS_FRAC_SCALE = float(_BC.get("loss_frac_scale", 0.5))

# ── P0-1 价值闸门强制 (2026-08-01, 针对 Flat Argmax 100%破产) ──
# PROD 环境强制: 所有下注必须经 compute_value_layer 审批 (value_layer_approved=True)。
# decide_argmax/decide_direction 裸调在 PROD 下直接返回 NO-BET (0 stake)。
# DEV 环境兼容旧行为 (不强制 value gate, 便于调试/对照实验)。
_VALUE_GATE_CONFIG = bool(_BC.get("value_gate_enforced", True))
VALUE_GATE_ENFORCED = (_ENV == "PROD") and _VALUE_GATE_CONFIG

# ── P0-2 回撤预算机制 (2026-08-01, 针对 Value Gate 34%回撤 + 76.8%水下时间) ──
# 动态Kelly缩放: 从峰值回撤越大 → Kelly比例线性缩至 dd_kelly_scale_min。
# 月度止盈止损: 当月累计P&L触及上下限 → 当月强制停注 (返回 NO-BET)。
_DD_ENABLED = bool(_BC.get("drawdown_budget_enabled", True))
DD_MAX_DRAWDOWN_PCT = float(_BC.get("max_drawdown_pct", 0.10))
DD_KELLY_SCALE_MIN = float(_BC.get("dd_kelly_scale_min", 0.25))
DD_MONTHLY_STOP_LOSS = float(_BC.get("monthly_stop_loss", -500.0))
DD_MONTHLY_STOP_PROFIT = float(_BC.get("monthly_stop_profit", 1500.0))


def _check_no_go(bet_count: int) -> Tuple[bool, str]:
    """ENV=PROD 容量护栏: 分歧子集 < MIN_DISAGREEMENT_BETS → 全局 NO-GO."""
    if _ENV == "PROD" and bet_count < MIN_DISAGREEMENT_BETS:
        return True, f"PROD NO-GO: disagreement_bets={bet_count}<{MIN_DISAGREEMENT_BETS}"
    return False, ""


def calc_drawdown_scale(current_equity: float, peak_equity: float,
                         max_drawdown_pct: float = DD_MAX_DRAWDOWN_PCT,
                         scale_min: float = DD_KELLY_SCALE_MIN) -> float:
    """P0-2 动态Kelly缩放: 基于峰值回撤线性缩放凯利比例.

    回撤 = 1 - current/peak。
      0% 回撤 → scale=1.0 (全凯利)
      max_drawdown_pct 回撤 → scale=scale_min
      超过 max_drawdown_pct → scale=scale_min (不继续降)

    Args:
        current_equity: 当前本金
        peak_equity: 历史峰值本金
        max_drawdown_pct: 触发最大缩放的回撤阈值 (默认 0.10=10%)
        scale_min: 最大回撤时的最低缩放比例 (默认 0.25)

    Returns:
        Kelly 缩放系数, 范围 [scale_min, 1.0]。
    """
    if peak_equity <= 0 or current_equity <= 0:
        return scale_min
    dd = 1.0 - current_equity / peak_equity
    if dd <= 0:
        return 1.0
    ratio = min(dd / max(max_drawdown_pct, 0.001), 1.0)
    return 1.0 - ratio * (1.0 - scale_min)


def check_monthly_limit(month_pnl: float,
                        stop_loss: float = DD_MONTHLY_STOP_LOSS,
                        stop_profit: float = DD_MONTHLY_STOP_PROFIT) -> tuple:
    """P0-2 月度止盈止损检查.

    Args:
        month_pnl: 当月累计盈亏 (可为负)
        stop_loss: 止损线 (负值), 当月P&L <= 此值触发
        stop_profit: 止盈线 (正值), 当月P&L >= 此值触发

    Returns:
        (should_stop: bool, reason: str).
    """
    if stop_loss < 0 and month_pnl <= stop_loss:
        return True, f"月度止损触发: P&L={month_pnl:.0f}<={stop_loss:.0f}"
    if stop_profit > 0 and month_pnl >= stop_profit:
        return True, f"月度止盈触发: P&L={month_pnl:.0f}>={stop_profit:.0f}"
    return False, ""


def kelly_fraction(p: float, odds: float) -> float:
    """满凯利注码比（占本金比例）. p=估计胜率, odds=十进制赔率. 负值截为0."""
    if odds <= 1.0:
        return 0.0
    b = odds - 1.0
    f = (p * odds - 1.0) / b
    return max(0.0, f)


def safe_stake(p: float, o: float, equity: float, frac_kelly: float = FRAC_KELLY, max_frac: float = MAX_STAKE_FRAC,
               source: str = "", gate: bool = False, bet_count: int = 0,
               spread_pp: Optional[float] = None, min_spread_pp: float = MIN_SPREAD_PP,
               consec_losses: int = 0, loss_cooldown_after: int = LOSS_COOLDOWN_AFTER,
               value_layer_approved: bool = False,
               edge_pct: Optional[float] = None,
               peak_equity: Optional[float] = None, month_pnl: float = 0.0) -> Tuple[float, float]:
    """规范凯利封顶注码 + 审计日志 + PROD NO-GO 护栏 + P0a 风控闸门 + P0-1/P0-2.

    Args:
        p, o, equity: 胜率/赔率/本金
        frac_kelly: 凯利比例 (默认 FRAC_KELLY)
        max_frac: 单注封顶比例 (默认 MAX_STAKE_FRAC)
        source: 调用来源 (用于审计追踪)
        gate: 分歧闸门是否通过 (gate=False 时强制 stake=0)
        bet_count: 分歧子集累计注数 (用于 PROD 容量护栏)
        spread_pp: 跨庄价差(pp); 配合 min_spread_pp 使用 (P0a-2, 默认不启用)
        min_spread_pp: 跨庄放注门槛(pp); >0时 spread_pp 不足则 NO-BET
        consec_losses: 当前连败场数; 配合 loss_cooldown_after 使用 (P0a-3, 默认不启用)
        loss_cooldown_after: 连败降档触发场数; >0时触发后 frac *= LOSS_FRAC_SCALE
        value_layer_approved: P0-1 价值闸门 — PROD 环境必须为 True (经 compute_value_layer 审批).
        peak_equity: P0-2 历史峰值本金 — 用于回撤动态缩放 (None=不启用, 等同于 current).
        month_pnl: P0-2 当月累计盈亏 — 用于月度止盈止损检查.

    Returns:
        (stake, kelly_fraction). 禁止下注时返回 (0.0, 0.0).

    P0a 风控 (2026-08-01, 针对 ROI=-30% 根因):
      1. 低赔热门限注 (默认启用): odds<HOT_ODDS_MAX 时单注压到 HOT_FRAC_CAP, 止"重注热门全输".
      1b. P0-3 热门主动拒止 (2026-08-11): 热门且 edge<HOT_EDGE_MIN_PP → NO-BET (FLB验证微边被抽水吃).
      2. 跨庄分歧门槛 (min_spread_pp>0 启用): 仅跨庄 spread 足够才放注 (真 edge 来自跨庄).
      3. 连败降档 (loss_cooldown_after>0 启用): 连输后凯利缩放, 防情绪化加注.

    P0-1 价值闸门 (2026-08-01, 针对 Flat Argmax 100%破产):
      PROD 环境强制 value_layer_approved=True. 裸调 decide_argmax 在 PROD 直接 NO-BET.

    P0-2 回撤预算 (2026-08-01, 针对 34%回撤 + 76.8%水下):
      动态Kelly缩放 + 月度止盈止损.
    """
    # ── PROD 容量护栏 ──
    no_go, no_go_reason = _check_no_go(bet_count)
    if no_go:
        _audit_logger.warning(f"NO-GO blocked: {no_go_reason} source={source}")
        return 0.0, 0.0

    # ── P0-1 价值闸门强制 (PROD 环境: 裸调 flat argmax/direction → NO-BET) ──
    if VALUE_GATE_ENFORCED and not value_layer_approved:
        _audit_logger.warning(
            f"NO-BET (P0-1 value gate): PROD requires value_layer_approved=True. "
            f"Use compute_value_layer instead of decide_argmax/direction. source={source}"
        )
        return 0.0, 0.0

    # ── 分歧闸门守卫 ──
    if not gate:
        _audit_logger.info(f"PASS: gate=False source={source}")
        return 0.0, 0.0

    # ── P0-2 月度止盈止损 (全局闸门, 在一切计算之前) ──
    if _DD_ENABLED:
        should_stop, stop_reason = check_monthly_limit(month_pnl)
        if should_stop:
            _audit_logger.warning(f"NO-BET (P0-2 monthly limit): {stop_reason} source={source}")
            return 0.0, 0.0

    # ── P0a-2 跨庄分歧门槛 (默认0不启用; P1接入跨庄后建议 min_spread_pp=15) ──
    if min_spread_pp > 0:
        if spread_pp is None or spread_pp < min_spread_pp:
            _audit_logger.info(f"NO_BET: spread_pp={spread_pp}<{min_spread_pp}pp source={source}")
            return 0.0, 0.0

    k = kelly_fraction(p, o)
    if k <= 0:
        _audit_logger.info(f"NO_BET: kelly={k:.4f} source={source}")
        return 0.0, 0.0

    frac = frac_kelly * k

    # ── P0-2 动态Kelly缩放 (基于峰值回撤) ──
    dd_scale = 1.0
    if _DD_ENABLED and peak_equity is not None and peak_equity > 0:
        dd_scale = calc_drawdown_scale(equity, peak_equity)
        if dd_scale < 1.0:
            frac *= dd_scale
            _audit_logger.info(
                f"DRAWDOWN_SCALE: dd={1.0 - equity / peak_equity:.1%} "
                f"scale={dd_scale:.3f} frac={frac:.4f} source={source}"
            )

    # ── P0a-3 连败降档 (默认0不启用) ──
    if loss_cooldown_after > 0 and consec_losses >= loss_cooldown_after:
        frac *= LOSS_FRAC_SCALE
        _audit_logger.info(f"LOSS_COOLDOWN: consec_losses={consec_losses}>={loss_cooldown_after} frac*={LOSS_FRAC_SCALE}")

    # ── P0a-1 低赔热门限注 (默认启用, 止血核心) ──
    # P0-3 热门主动拒止: 热门且 edge < HOT_EDGE_MIN_PP → 直接 NO-BET (微边被抽水吃)
    hot_capped = False
    if o < HOT_ODDS_MAX:
        if edge_pct is not None and edge_pct < HOT_EDGE_MIN_PP:
            _audit_logger.warning(
                f"NO-BET (P0-3 hot pass): odds={o:.2f}<{HOT_ODDS_MAX} edge={edge_pct:.1f}pp<{HOT_EDGE_MIN_PP}pp "
                f"source={source}"
            )
            return 0.0, 0.0
        if frac > HOT_FRAC_CAP:
            frac = HOT_FRAC_CAP
            hot_capped = True

    cap_hit = False
    if frac > max_frac:
        frac = max_frac
        cap_hit = True

    stake = frac * equity
    if stake <= 0 or stake > equity:
        _audit_logger.warning(f"ILLEGAL: stake={stake:.2f} equity={equity:.2f} source={source}")
        return 0.0, 0.0

    _audit_logger.info(
        f"BET: kelly={k:.4f} frac={frac:.4f} cap_hit={cap_hit} hot_capped={hot_capped} "
        f"dd_scale={dd_scale:.3f} value_approved={value_layer_approved} "
        f"spread_pp={spread_pp} gate={gate} source={source} stake={stake:.2f} equity={equity:.2f}"
    )
    return stake, k


def decide_direction(direction_idx: int, p_vec: List[float], odds: List[float], equity: float, winner: str,
                     gate: bool = True, value_layer_approved: bool = False,
                     peak_equity: Optional[float] = None, month_pnl: float = 0.0) -> Tuple[float, float, bool]:
    """按指定方向下注(价值层/任意信号): 规范半凯利(封顶), 返回(新equity, stake, win).

    注: gate 默认 True。本函数调用方均为「价值层已判 BET / argmax 已定方向」
    的下游下注入口, 默认即允许下注; 须经分歧闸门过滤的调用方(如 live_pilot
    的 no-gate 对照)显式传 gate=False。gate=False 会强制 stake=0 (不下注)。

    P0-1: PROD 环境 value_layer_approved 必须为 True (经 compute_value_layer 审批),
    否则 safe_stake 返回 NO-BET。DEV 环境 value_layer_approved 不强制 (向后兼容).
    """
    p = p_vec[direction_idx]
    o = odds[direction_idx]
    stake, _ = safe_stake(p, o, equity, gate=gate, value_layer_approved=value_layer_approved,
                          peak_equity=peak_equity, month_pnl=month_pnl)
    if stake <= 0:
        return equity, 0.0, False
    d = ("H", "D", "A")[direction_idx]
    if winner == d:
        return equity + stake * (o - 1), stake, True
    return equity - stake, stake, False


def decide_argmax(p_vec: List[float], odds: List[float], equity: float, winner: str,
                  gate: bool = True, value_layer_approved: bool = False,
                  peak_equity: Optional[float] = None, month_pnl: float = 0.0) -> Tuple[float, float, bool]:
    """argmax 方向下注(共识/押热门): 底层复用 decide_direction.

    P0-1: PROD 环境 value_layer_approved=False 时 safe_stake 直接返回 NO-BET。
    调用方应始终经 compute_value_layer 审批后传 value_layer_approved=True。
    """
    i = int(max(range(3), key=lambda j: p_vec[j]))
    return decide_direction(i, p_vec, odds, equity, winner, gate=gate,
                            value_layer_approved=value_layer_approved,
                            peak_equity=peak_equity, month_pnl=month_pnl)
