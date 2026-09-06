"""
操盘手逆转信号 — 运行时推理模块 v1.0

从 odds_features 学到的核心信号:
  1. 平局漂移(drift_d) — 最强逆转信号
  2. 赔率差变化(open→close spread) — 不确定性放大=逆转概率上升
  3. 隐含概率偏移(imp_shift) — 操盘手真金白银的方向

输入: 开盘赔率(oh,od,oa) + 终盘赔率(ch,cd,ca)
输出: 逆转风险分(0-1) + 操盘手方向信号 + 关键信号明细

用法:
    from pipeline.operator_signals import operator_signal
    sig = operator_signal(oh=2.10, od=3.15, oa=3.40, ch=1.95, cd=3.25, ca=3.80)
    # sig = {"reversal_risk": 0.62, "direction": "fade_home", "drift_draw_down": True, ...}
"""

import numpy as np
from pathlib import Path
import os
import math
from enum import Enum
from typing import Optional, Dict, Any

_MODEL_DIR = Path(__file__).resolve().parent.parent / "saved_models"
_rev_model = None
_reliability_model = None

def _load_models():
    global _rev_model, _reliability_model
    if _rev_model is not None:
        return
    import joblib
    _rev_model = joblib.load(str(_MODEL_DIR / "operator_reversal_detector.joblib"))
    _reliability_model = joblib.load(str(_MODEL_DIR / "operator_drift_reliability.joblib"))


def _parse_score(s: str) -> tuple:
    """'1-4' -> (1, 4); 解析失败回落 (0, 0)。"""
    try:
        a, b = str(s).split("-")
        return int(float(a)), int(float(b))
    except Exception:
        return 0, 0


def _poisson_pmf(lam: float, k: int) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _comeback_feasibility(current_score: str, current_minute: int) -> float:
    """以「当前比分 + 剩余时间」为贝叶斯先验, 估算落后方翻盘(净胜≥deficit+1)的可行性(0-1)。

    报告根因: 逆转信号若只吃开盘→收盘赔率漂移, 在 1-4@87' 这种大比分倾斜后期
    仍会输出 63% 逆转风险, 与事实(客队几乎锁定胜局)系统性背离。
    修复: 用 in-play Poisson 卷积, 落后方 desperate 进攻速率(0.13 球/分×1.4) vs
    领先方防守收缩(0.09 球/分), 在剩余分钟内净胜≥(deficit+1)的概率即翻盘可行性。
    1-4@87'(剩3分, deficit=3, need=4) → 落后方期望仅0.55球 → 可行性≈0, 逆转风险被压死。
    """
    sh, sa = _parse_score(current_score)
    deficit = abs(sh - sa)
    if deficit == 0:
        return 1.0  # 平局仍有翻盘/取胜空间, 不裁
    remain = max(0.0, 90.0 - float(current_minute))
    if remain <= 0:
        return 0.0  # 已结束(或无剩余时间), 比分即最终结果
    rt = 0.13 * remain * 1.4   # 落后方 desperate 进攻总期望
    rl = 0.09 * remain         # 领先方防守收缩总期望
    need = deficit + 1          # 净胜≥deficit+1 才翻盘获胜
    p = 0.0
    for jt in range(0, 30):
        pt = _poisson_pmf(rt, jt)
        if pt < 1e-12:
            continue
        for jl in range(0, 30):
            if jt - jl >= need:
                p += pt * _poisson_pmf(rl, jl)
    return min(1.0, max(0.0, p))


def operator_signal(oh: float, od: float, oa: float,
                    ch: float = 0, cd: float = 0, ca: float = 0,
                    current_score: str = "0-0", current_minute: int = 0) -> dict:
    """单场操盘手信号 — 从初盘→终盘漂移提取逆转风险。

    若无终盘(仅开盘): ch/cd/ca 传 0 → 仅基于开盘价和已有 sigma 推算。

    current_score / current_minute: in-play 比分与时间(可选)。传入后逆转风险按
    落后方翻盘可行性(以比分+剩余时间为先验)缩放, 避免大比分后期仍报高逆转率。
    缺省 '0-0'/0 → 不施加比分条件(赛前/无live场景)。

    返回:
        reversal_risk: 0-1 逆转概率(已按比分可行性缩放)
        score_feasibility: 翻盘可行性(0-1), =1 表示未受比分限制
        score_conditioned: 本次是否因比分被压低
        drift_dir / drift_draw_down / spread_change / signals: 同前
    """
    _load_models()

    # 无终盘 → 假设零漂移 (赛前场景)
    if ch <= 0 or cd <= 0 or ca <= 0:
        ch, cd, ca = oh, od, oa

    # 比分条件化: 落后方翻盘可行性(以当前比分+剩余时间为先验)
    score_feas = _comeback_feasibility(current_score, current_minute)
    score_conditioned = score_feas < 0.999

    # 计算漂移
    dh = (ch - oh) / oh if oh > 0 else 0
    dd = (cd - od) / od if od > 0 else 0
    da = (ca - oa) / oa if oa > 0 else 0

    # 去水概率
    inv_open = 1.0/oh + 1.0/od + 1.0/oa
    p_open = [(1.0/oh)/inv_open, (1.0/od)/inv_open, (1.0/oa)/inv_open]
    inv_close = 1.0/ch + 1.0/cd + 1.0/ca
    p_close = [(1.0/ch)/inv_close, (1.0/cd)/inv_close, (1.0/ca)/inv_close]

    # 开盘/收盘最被看好方
    open_fav = np.argmin([oh, od, oa])
    close_fav = np.argmin([ch, cd, ca])

    # 隐含概率偏移
    imp_shift = [p_close[i] - p_open[i] for i in range(3)]

    # 赔率差
    open_spread = max(oh, od, oa) - min(oh, od, oa)
    close_spread = max(ch, cd, ca) - min(ch, cd, ca)

    # 漂移强度
    drift_mag = abs(dh) + abs(dd) + abs(da)
    drift_dir = int(np.argmin([dh, dd, da]))

    # 构建特征
    feats = np.array([[
        p_open[0], p_open[1], p_open[2],
        p_close[0], p_close[1], p_close[2],
        dh, dd, da,
        imp_shift[0], imp_shift[1], imp_shift[2],
        open_spread, close_spread,
        0.0,  # sigma_trap (live无历史数据)
        float(open_fav == close_fav),
        drift_mag,
        float(drift_dir),
    ]], dtype=np.float32)

    # 推理
    rev_proba_raw = float(_rev_model.predict_proba(feats)[0, 1])
    # 比分条件化: 翻盘不可行时压低逆转风险(避免1-4@87'仍报63%)
    rev_proba = rev_proba_raw * score_feas
    rel_proba = float(_reliability_model.predict_proba(feats)[0, 1])

    # 信号明细
    signals = []
    if dd < -0.03:
        signals.append("drift_draw_down")  # 平赔被拉低→平局概率升
    if dd > 0.03:
        signals.append("drift_draw_up")    # 平赔被抬高→大概率分胜负
    if drift_mag > 0.05:
        signals.append("drift_significant")
    if open_fav != close_fav:
        signals.append("favorite_flip")
    if (close_spread - open_spread) > 0.5:
        signals.append("spread_widening")

    # 方向信号 — 仅在存在有效漂移时输出; 零漂移(开盘=收盘, Δ全0)不臆造方向
    # (铁律: 陷阱/方向判定须有开盘→收盘漂移(drift)证据, 无 drift 时方向不可信)
    DRIFT_GATE = 0.02  # 三方向漂移绝对值之和下限; 低于此视为无有效漂移
    drift_present = drift_mag >= DRIFT_GATE
    dir_map = {0: "home", 1: "draw", 2: "away"}
    direction = dir_map.get(drift_dir, "unknown") if drift_present else "none"

    # 比分条件化标注: 翻盘可行性极低时, 记录原因并提示"信号不可信"
    if score_conditioned and score_feas < 0.05:
        signals.append(f"score_capped(reversal blocked: deficit, remain={int(90-float(current_minute))}min)")
    elif score_conditioned:
        signals.append(f"score_capped(feas={score_feas:.2f})")

    # 2026-08-12 反推复盘: 可靠性无下限(如37%)仍完整呈现会误导. 加 floor,
    # 低于阈值标 reliability_low(前端折叠/标注"低可靠性, 仅供参考"), 但不强行改 direction.
    RELIABILITY_FLOOR = 0.30
    reliability_low = rel_proba < RELIABILITY_FLOOR

    return {
        "reversal_risk": round(rev_proba, 4),
        "reversal_risk_raw": round(rev_proba_raw, 4),
        "score_feasibility": round(score_feas, 4),
        "score_conditioned": bool(score_conditioned),
        "operator_reliability": round(rel_proba, 4),
        "reliability_low": bool(reliability_low),
        "direction": direction,
        "drift_present": drift_present,
        "drift_draw_down": bool(dd < -0.03),
        "drift_significant": bool(drift_mag > 0.05),
        "favorite_flip": bool(open_fav != close_fav),
        "spread_change": round(close_spread - open_spread, 2),
        "signals": signals,
        "delta": {"h": round(dh, 4), "d": round(dd, 4), "a": round(da, 4)},
    }


# ═══════════════════════════════════════════════════════════════════════════
# 事故⑤ 低水双态状态机 (REQ-07, T07 + T13)
#
# 根因: live 场景无开盘价截图时, 低水线被直接判"诱多(TRAP)", 无依据打脸
#       (特尔纳瓦 2-2). 双态状态机强制: 无开盘价 → NEUTRAL/待确认, 禁 TRAP.
#
# 设计:
#   feed(opening_price, current_price=None):
#     opening_price is None → NEED_OPENING (待确认, 永不 TRAP)
#     否则 → HAVE_OPENING, 跑完整陷阱/价值逻辑 (需 current_price 才算漂移)
#   decide():
#     NEED_OPENING → Verdict.NEUTRAL (confidence 低, 标注"依据不足")
#     HAVE_OPENING → 低水陷阱/价值判定 (带 source + confidence)
# ═══════════════════════════════════════════════════════════════════════════

class LowWaterState(Enum):
    """低水状态机状态。"""
    NEED_OPENING = "NEED_OPENING"   # 开盘价缺失 → 待确认, 禁 TRAP
    HAVE_OPENING = "HAVE_OPENING"   # 有开盘价 → 跑完整逻辑


class Verdict(Enum):
    """低水判定结论。"""
    NEUTRAL = "NEUTRAL"
    VALUE = "VALUE"
    TRAP = "TRAP"


class Source(Enum):
    """信号来源 (REQ-13 来源标注)。"""
    LEYU = "LEYU"
    LEISU = "LEISU"
    UNIFIED = "UNIFIED"
    DISPUTED = "DISPUTED"


# 低水线阈值 (亚盘水位等效; <=此值视为"低水"/被看好方)
_LOW_WATER_THRESHOLD = 0.90
# 低水继续走低超过此漂移 → 操盘手压低诱导(TRAP)
_TRAP_DRIFT = -0.03
# 低水走高超过此漂移 → 价值释放(VALUE)
_VALUE_DRIFT = 0.03
# 非低水侧的一般方向漂移门槛
_GENERIC_DRIFT = 0.05


class LowWaterStateMachine:
    """低水双态状态机 — 无开盘价绝不判诱多(TRAP)。

    用法:
        sm = LowWaterStateMachine(match_id="m1")
        sm.feed(opening_price=None)          # live 无开盘价
        out = sm.decide()                     # → NEUTRAL, 不 TRAP
        sm.feed(opening_price=0.80, current_price=0.75)
        out = sm.decide()                     # → TRAP/VALUE/NEUTRAL (带 source+confidence)
    """

    def __init__(self, match_id: str = "", market: str = "ah") -> None:
        self._match_id = match_id
        self._market = market
        self._state = LowWaterState.NEED_OPENING
        self._opening_price: Optional[float] = None
        self._current_price: Optional[float] = None

    def feed(self, opening_price: Optional[float],
             current_price: Optional[float] = None) -> None:
        """喂入盘口价格, 决定状态。

        Args:
            opening_price: 开盘价 (亚盘水位/等效低水线); None → NEED_OPENING。
            current_price: 即时(live)价; 缺省视为等于开盘价(零漂移)。
        """
        if opening_price is None:
            # 无开盘价: 永远 NEED_OPENING, 禁止后续判 TRAP (特尔纳瓦打脸根因闭环)
            self._state = LowWaterState.NEED_OPENING
            self._opening_price = None
        else:
            self._state = LowWaterState.HAVE_OPENING
            self._opening_price = float(opening_price)
        self._current_price = (
            None if current_price is None else float(current_price)
        )

    @property
    def state(self) -> LowWaterState:
        """当前状态 (供测试/可解释视图读取)。"""
        return self._state

    def decide(self) -> Dict[str, Any]:
        """给出低水判定 (带 source + confidence)。

        返回 dict: {match_id, signal_type, verdict, source, confidence, value, roi, basis}
          - NEED_OPENING → Verdict.NEUTRAL (confidence 低, source=UNIFIED, basis 标注依据不足)
          - HAVE_OPENING → 完整陷阱/价值逻辑
        """
        # ── NEED_OPENING: 固定 NEUTRAL/待确认, 绝不 TRAP ──
        if self._state is LowWaterState.NEED_OPENING:
            return {
                "match_id": self._match_id,
                "signal_type": "low_water",
                "verdict": Verdict.NEUTRAL.value,
                "source": Source.UNIFIED.value,
                "confidence": 0.2,
                "value": 0.0,
                "roi": 0.0,
                "basis": "依据不足: 无开盘价, live 场景禁止判诱多(TRAP)",
            }

        # ── HAVE_OPENING: 完整陷阱/价值逻辑 ──
        opening = self._opening_price
        current = opening if self._current_price is None else self._current_price
        drift = (current - opening) / opening if opening else 0.0

        if opening <= _LOW_WATER_THRESHOLD:
            # 低水线: 即时继续走低 → 操盘手压低诱导(TRAP); 走高 → 价值释放(VALUE)
            if drift <= _TRAP_DRIFT:
                verdict = Verdict.TRAP
                confidence = 0.7
                basis = (f"低水线 opening={opening:.3f} → current={current:.3f} "
                         f"(drift={drift:+.2%}) 继续走低, 操盘手压低诱导")
            elif drift >= _VALUE_DRIFT:
                verdict = Verdict.VALUE
                confidence = 0.6
                basis = (f"低水线 opening={opening:.3f} → current={current:.3f} "
                         f"(drift={drift:+.2%}) 走高, 价值释放")
            else:
                verdict = Verdict.NEUTRAL
                confidence = 0.5
                basis = f"低水线 opening={opening:.3f} → current={current:.3f} 即时平稳"
        else:
            # 非低水: 用一般漂移方向判 TRAP/VALUE
            if drift <= -_GENERIC_DRIFT:
                verdict = Verdict.TRAP
                confidence = 0.55
                basis = (f"赔率走低 opening={opening:.3f} → {current:.3f} "
                         f"(drift={drift:+.2%}), 疑似诱导")
            elif drift >= _GENERIC_DRIFT:
                verdict = Verdict.VALUE
                confidence = 0.5
                basis = (f"赔率走高 opening={opening:.3f} → {current:.3f} "
                         f"(drift={drift:+.2%}), 价值")
            else:
                verdict = Verdict.NEUTRAL
                confidence = 0.45
                basis = f"盘口平稳 opening={opening:.3f} → {current:.3f}"

        return {
            "match_id": self._match_id,
            "signal_type": "low_water",
            "verdict": verdict.value,
            "source": Source.UNIFIED.value,
            "confidence": confidence,
            "value": round(drift, 4),
            "roi": 0.0,
            "basis": basis,
        }
