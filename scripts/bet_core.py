#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bet_core: 统一投注核心 (P0-2 / P0-3 / live_pilot 单一事实源).

把分散在 p0_2.decide / p0_3.decide_dir 的重复下注逻辑收敛到一处, 杜绝公式漂移:
  - kelly_fraction 封装 (规范满凯利分数 (p*o-1)/(o-1); 已修正 P0-3 误用 0.5*(p*o-1) 当分数的坑)
  - 规范半凯利 (FRAC_KELLY=0.5) + 单注封顶 10% (MAX_STAKE_FRAC; P0-2 修复的 kelly>1 全押 bug)
  - decide_argmax / decide_direction 两个方向入口, 底层同实现

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
FRAC_KELLY = float(_BC.get("frac_kelly", 0.5))
MAX_STAKE_FRAC = float(_BC.get("max_stake_frac", 0.10))
MIN_DISAGREEMENT_BETS = int(_BC.get("min_disagreement_bets", 300))
_ENV = str(_BC.get("env", "DEV")).upper()


def _check_no_go(bet_count: int) -> Tuple[bool, str]:
    """ENV=PROD 容量护栏: 分歧子集 < MIN_DISAGREEMENT_BETS → 全局 NO-GO."""
    if _ENV == "PROD" and bet_count < MIN_DISAGREEMENT_BETS:
        return True, f"PROD NO-GO: disagreement_bets={bet_count}<{MIN_DISAGREEMENT_BETS}"
    return False, ""


def kelly_fraction(p: float, odds: float) -> float:
    """满凯利注码比（占本金比例）. p=估计胜率, odds=十进制赔率. 负值截为0."""
    if odds <= 1.0:
        return 0.0
    b = odds - 1.0
    f = (p * odds - 1.0) / b
    return max(0.0, f)


def safe_stake(p: float, o: float, equity: float, frac_kelly: float = FRAC_KELLY, max_frac: float = MAX_STAKE_FRAC,
               source: str = "", gate: bool = False, bet_count: int = 0) -> Tuple[float, float]:
    """规范凯利封顶注码 + 审计日志 + PROD NO-GO 护栏.

    Args:
        p, o, equity: 胜率/赔率/本金
        frac_kelly: 凯利比例 (默认 FRAC_KELLY)
        max_frac: 单注封顶比例 (默认 MAX_STAKE_FRAC)
        source: 调用来源 (用于审计追踪)
        gate: 分歧闸门是否通过 (gate=False 时强制 stake=0)
        bet_count: 分歧子集累计注数 (用于 PROD 容量护栏)

    Returns:
        (stake, kelly_fraction). 禁止下注时返回 (0.0, 0.0).

    审计日志: 记录 kelly/frac/cap_hit/gate/source/bet_count 到 bet_core.audit.
    """
    # ── PROD 容量护栏 ──
    no_go, no_go_reason = _check_no_go(bet_count)
    if no_go:
        _audit_logger.warning(f"NO-GO blocked: {no_go_reason} source={source}")
        return 0.0, 0.0

    # ── 分歧闸门守卫 ──
    if not gate:
        _audit_logger.info(f"PASS: gate=False source={source}")
        return 0.0, 0.0

    k = kelly_fraction(p, o)
    if k <= 0:
        _audit_logger.info(f"NO_BET: kelly={k:.4f} source={source}")
        return 0.0, 0.0

    frac = frac_kelly * k
    cap_hit = False
    if frac > max_frac:
        frac = max_frac
        cap_hit = True

    stake = frac * equity
    if stake <= 0 or stake > equity:
        _audit_logger.warning(f"ILLEGAL: stake={stake:.2f} equity={equity:.2f} source={source}")
        return 0.0, 0.0

    _audit_logger.info(
        f"BET: kelly={k:.4f} frac={frac:.4f} cap_hit={cap_hit} "
        f"gate={gate} source={source} stake={stake:.2f} equity={equity:.2f}"
    )
    return stake, k


def decide_direction(direction_idx: int, p_vec: List[float], odds: List[float], equity: float, winner: str, gate: bool = True) -> Tuple[float, float, bool]:
    """按指定方向下注(价值层/任意信号): 规范半凯利(封顶), 返回(新equity, stake, win).

    注: gate 默认 True。本函数调用方均为「价值层已判 BET / argmax 已定方向」
    的下游下注入口, 默认即允许下注; 须经分歧闸门过滤的调用方(如 live_pilot
    的 no-gate 对照)显式传 gate=False。gate=False 会强制 stake=0 (不下注)。
    """
    p = p_vec[direction_idx]
    o = odds[direction_idx]
    stake, _ = safe_stake(p, o, equity, gate=gate)
    if stake <= 0:
        return equity, 0.0, False
    d = ("H", "D", "A")[direction_idx]
    if winner == d:
        return equity + stake * (o - 1), stake, True
    return equity - stake, stake, False


def decide_argmax(p_vec: List[float], odds: List[float], equity: float, winner: str, gate: bool = True) -> Tuple[float, float, bool]:
    """argmax 方向下注(共识/押热门): 底层复用 decide_direction."""
    i = int(max(range(3), key=lambda j: p_vec[j]))
    return decide_direction(i, p_vec, odds, equity, winner, gate=gate)
