# -*- coding: utf-8 -*-
"""
输出偏置校准层 (Calibration Overlay) v1.0
==========================================
基于 tick 信号(31.5万行赛果验证) + OU 阈值信号(14463场验证)的后处理偏置。
不是特征矩阵输入(树模型已从裸赔率学到tick, 28维=22维), 而是输出概率的校准修正。

信号来源(均经二项检验 p<0.001):
  tick (1X2 1.0-1.49大热门区, 覆盖8.6%):
    .4 尾数 = 陷阱: 该方赢率 -6.8pp(主) / -8.1pp(客)
    .1/.2/.9 尾数 = 强信号: 该方赢率 +7.1~14.6pp
  OU阈值 (≤1.75, 覆盖~51%):
    over ≤1.75: over命中率 +8.3pp
    under ≤1.75: under命中率 +10.3pp

用法:
  from pipeline.calibration_overlay import apply_1x2_overlay, apply_ou_overlay
  ph, pd, pa = apply_1x2_overlay(ph, pd, pa, oh, od, oa)
  p_over, p_under = apply_ou_overlay(p_over, p_under, over_odds, under_odds)
"""
import math
from typing import Tuple

# ── tick偏置参数 (来自31.5万行赛果验证) ──
TICK_REGION = (1.0, 1.5)        # 仅大热门区有效
TICK_TRAP = {4}                  # 陷阱尾数: 该方赢率显著低
TICK_STRONG = {1, 2, 9}          # 强信号尾数: 该方赢率显著高
TICK_TRAP_DELTA = -0.07          # 陷阱: 该方概率下调(主-6.8/客-8.1pp, 取中值-7pp)
TICK_STRONG_DELTA = 0.08         # 强信号: 该方概率上调(主+7.1/客+8.2pp, 取+8pp)

# ── OU偏置参数 (来自14463场赛果验证) ──
OU_FAVOR_THRESHOLD = 1.75        # 赔率≤此值=庄家偏向该方(强信号)
OU_OVER_DELTA = 0.08             # over≤1.75: over概率+8.3pp
OU_UNDER_DELTA = 0.10            # under≤1.75: under概率+10.3pp

# ── 偏置触发标记 (供前端/日志展示, 不影响计算) ──
OVERLAY_APPLIED = []  # 记录本次调用了哪些偏置


def _tick(odds: float) -> int:
    """赔率的百分位尾数(0-9)。"""
    try:
        return int(round((float(odds) * 100) % 10)) % 10
    except Exception:
        return -1


def _in_tick_region(odds) -> bool:
    try:
        return TICK_REGION[0] <= float(odds) < TICK_REGION[1]
    except Exception:
        return False


def apply_1x2_overlay(ph: float, pd: float, pa: float,
                      oh=None, od=None, oa=None) -> Tuple[float, float, float, list]:
    """1X2 输出概率的 tick 偏置后处理。

    Args:
      ph/pd/pa: 去抽水隐含概率(或模型概率), 和为1
      oh/od/oa: 1X2 收盘赔率(用于算tick)
    Returns:
      (ph', pd', pa', applied) — 修正后概率 + 触发的偏置列表
    """
    applied = []
    if ph is None or pd is None or pa is None:
        return ph, pd, pa, applied
    dh = da = 0.0
    # 主胜 tick 偏置
    if oh and _in_tick_region(oh):
        t = _tick(oh)
        if t in TICK_TRAP:
            dh += TICK_TRAP_DELTA; applied.append(f"home_trap_.{t}({TICK_TRAP_DELTA:+.2f})")
        elif t in TICK_STRONG:
            dh += TICK_STRONG_DELTA; applied.append(f"home_strong_.{t}({TICK_STRONG_DELTA:+.2f})")
    # 客胜 tick 偏置
    if oa and _in_tick_region(oa):
        t = _tick(oa)
        if t in TICK_TRAP:
            da += TICK_TRAP_DELTA; applied.append(f"away_trap_.{t}({TICK_TRAP_DELTA:+.2f})")
        elif t in TICK_STRONG:
            da += TICK_STRONG_DELTA; applied.append(f"away_strong_.{t}({TICK_STRONG_DELTA:+.2f})")
    if not applied:
        return ph, pd, pa, applied
    # 应用偏置 + 重归一化
    ph2 = max(0.01, ph + dh)
    pa2 = max(0.01, pa + da)
    # 平局不直接偏置, 但受归一化影响
    s = ph2 + pd + pa2
    return round(ph2/s, 4), round(pd/s, 4), round(pa2/s, 4), applied


def apply_ou_overlay(p_over: float, p_under: float,
                     over_odds=None, under_odds=None) -> Tuple[float, float, list]:
    """OU 输出概率的阈值偏置后处理。

    ⚠️ 回测结论(2026-08-03, 14431场): OU≤1.75的+8.3pp命中率偏差,
       去抽水隐含概率已隐含此信息, 再加偏置=重复加权, Brier反而变差(+0.18)。
       幅度扫描0~0.10全部劣于基线。故本函数默认 no-op(返回原值)。
       OU信号是"方向命中率"信号, 不是"概率校准"信号 — 不适合后处理偏置。
    """
    # 默认不偏置(回测证伪)。保留函数签名供未来重新评估。
    return p_over, p_under, []
