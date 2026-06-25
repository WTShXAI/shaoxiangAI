"""
DrawGate v5.3 — 平局专用识别模块 (路线B)
===========================================
合并 D-Gate v5.2 风险检测 + DrawExpert v1 平局预测, 专攻平局识别。

设计原则 (赵统筹 B方案):
  ✅ 不抢 argmax 的活 — 不强制改判
  ✅ 风险标记 + 置信度衰减 + 平局抬权
  ✅ 杯赛/联赛双参数集
  ✅ C/C-away 触发 → 降权强队, 给平局更大空间
  ✅ A/B 触发 → DrawExpert 增强, 目标 D-F1>0.15

架构 (在 UnifiedPredictor 中的位置):
  [L1] SKY Stacking → [L2] λ Fusion → [L3] Trap → [L4] DrawGate → [L5] Threshold

返回:
  {
    "risk_tag": "upset_warning" | "draw_alert" | "clean",
    "draw_threshold_adj": float,      # 调整后的平局阈值 (e.g. 0.32→0.22)
    "confidence_mult": float,         # 强队置信度衰减倍数 (e.g. 0.85)
    "draw_boost": float,              # DrawExpert 信号额外boost
    "dgate_mode": str,                # C/C-away/A/B/none
    "triggered_signals": [str],
  }
"""

import json, math, logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger("DrawGate")

# ── 赛事规则路径 ──
_RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "drawgate_v53_rules.json"

# ═══════════════════════════════════════════════════════════
# 默认规则 (硬编码兜底: 杯赛 + 联赛)
# ═══════════════════════════════════════════════════════════
_DEFAULT_RULES = {
    "tournament": {
        "mode_c": {
            "imp_min": 0.72,          # 隐含胜率 >72%
            "draw_threshold_drop": 0.10,   # 阈值从0.32→0.22
            "confidence_decay": 0.85,      # 强队置信度×0.85
            "draw_boost": 0.06,            # DrawExpert额外+6%
            "od_max": 6.0,                 # 平赔上限 (异常信号)
        },
        "mode_c_away": {
            "pa_min": 0.65,           # 客队隐含胜率 >65%
            "draw_threshold_drop": 0.08,
            "confidence_decay": 0.88,
            "draw_boost": 0.04,
        },
        "mode_a": {
            "imp_min": 0.48,
            "imp_max": 0.72,
            "spread_power": 0.30,
            "ou_boost": 1.05,
            "draw_threshold_drop": 0.05,
            "confidence_decay": 0.92,
            "draw_boost": 0.05,
            "s7s1_penalty": 0.70,
            "s7_threshold": 3.5,
            "s1_threshold": 1.30,
            "threshold": 0.28,
        },
        "mode_b": {
            "spread_max": 0.20,       # 杯赛保守用0.20
            "draw_threshold_drop": 0.04,
            "confidence_decay": 0.95,
            "draw_boost": 0.04,
            "threshold": 0.43,
            "boost": 1.20,
        },
    },
    "league": {
        "mode_c": {
            "imp_min": 0.72,
            "draw_threshold_drop": 0.08,
            "confidence_decay": 0.87,
            "draw_boost": 0.04,
            "od_max": 6.0,
        },
        "mode_c_away": {
            "pa_min": 0.65,
            "draw_threshold_drop": 0.06,
            "confidence_decay": 0.90,
            "draw_boost": 0.03,
        },
        "mode_a": {
            "imp_min": 0.38,          # 联赛降低下限 48→38 (弱队对决也有平局)
            "imp_max": 0.70,
            "spread_power": 0.25,     # 联赛 spread抑制更强
            "ou_boost": 1.03,
            "draw_threshold_drop": 0.05,
            "confidence_decay": 0.92,
            "draw_boost": 0.04,
            "s7s1_penalty": 0.65,
            "s7_threshold": 3.5,
            "s1_threshold": 1.25,
            "threshold": 0.26,        # 联赛触发门槛更低
        },
        "mode_b": {
            "spread_max": 0.22,       # 联赛放宽到0.22
            "draw_threshold_drop": 0.04,
            "confidence_decay": 0.95,
            "draw_boost": 0.03,
            "threshold": 0.40,        # 联赛门槛略降
            "boost": 1.15,
        },
    },
}


def _load_rules() -> Dict:
    """加载规则 (JSON > 默认硬编码)"""
    try:
        with open(_RULES_PATH, 'r', encoding='utf-8') as f:
            rules = json.load(f)
        logger.info(f"[DrawGate v5.3] 规则加载: {_RULES_PATH}")
        return rules
    except Exception:
        logger.info("[DrawGate v5.3] 使用默认规则")
        return _DEFAULT_RULES


# ═══════════════════════════════════════════════════════════
# 赛事类型检测
# ═══════════════════════════════════════════════════════════
_TOURNAMENT_KW = [
    "世界杯", "world cup", "欧洲杯", "euro", "美洲杯", "copa",
    "亚洲杯", "asian cup", "非洲杯", "afcon", "欧冠", "champions league",
    "欧联", "europa", "杯赛", "锦标赛", "淘汰赛", "小组赛",
    "round of", "group", "knockout", "final", "semi",
]
_LEAGUE_KW = [
    "英超", "premier league", "西甲", "la liga", "意甲", "serie a",
    "德甲", "bundesliga", "法甲", "ligue 1", "中超", "联赛", "league",
    "英冠", "championship", "荷甲", "eredivisie", "葡超",
]


def detect_match_type(league_name: str = "") -> str:
    text = league_name.lower()
    for kw in _TOURNAMENT_KW:
        if kw in text:
            return "tournament"
    for kw in _LEAGUE_KW:
        if kw in text:
            return "league"
    return "tournament"  # 默认杯赛 (保守)


# ═══════════════════════════════════════════════════════════
# DrawGate v5.3 核心
# ═══════════════════════════════════════════════════════════

def apply_drawgate(
    imp_h: float,
    imp_d: float,
    imp_a: float,
    odds: Dict[str, float],
    handicap: Optional[float] = None,
    ou_line: Optional[float] = None,
    match_type: str = "tournament",
    draw_expert_signal: Optional[float] = None,
    lambda_residual: Optional[float] = None,
    odds_slope: Optional[float] = None,
) -> Dict[str, Any]:
    """
    DrawGate v5.3 — 平局专用识别

    核心改动 vs D-Gate v5.2:
      - 不强制改判 → 返回 risk_tag + 阈值调整 + 置信度衰减
      - DrawExpert 信号参与 boost 计算
      - λ残差/赔率斜率作为辅助触发 (可选, v5.3+)

    Args:
        imp_h/d/a: 隐含概率 (从赔率反推)
        odds: {'home': float, 'draw': float, 'away': float}
        handicap: 亚盘让球 (正=客让)
        ou_line: 大小球盘口
        match_type: 'tournament' | 'league'
        draw_expert_signal: DrawExpert v1 输出 (0~1)
        lambda_residual: λ反演残差 (v5.3+, 可选)
        odds_slope: 赔率斜率 T-4h→T-1h (v5.3+, 可选)

    Returns:
        dict with risk_tag, draw_threshold_adj, confidence_mult, draw_boost, dgate_mode, signals
    """
    rules = _load_rules()
    cfg = rules.get(match_type, rules.get("tournament", {}))
    mcc = cfg.get("mode_c", {})
    mca = cfg.get("mode_c_away", {})
    ma = cfg.get("mode_a", {})
    mb = cfg.get("mode_b", {})

    oh = odds.get('home', 2.0)
    od = odds.get('draw', 3.2)
    oa = odds.get('away', 2.0)
    spread = abs(imp_h - imp_a)
    max_imp = max(imp_h, imp_a)
    hcp = handicap or 0.0
    ou = ou_line or 2.5

    # ── 默认输出 ──
    result = {
        "risk_tag": "clean",
        "draw_threshold_adj": 0.32,      # 默认不变
        "confidence_mult": 1.0,           # 默认不衰减
        "draw_boost": 0.0,
        "dgate_mode": "none",
        "triggered_signals": [],
    }

    # ═══ 赔率深层信号 ═══
    s1_draw_cheapness = od / math.sqrt(oh * oa) if oh > 0 and oa > 0 else 1.0
    s7_ou_hcp_ratio = ou / max(abs(hcp), 0.25)

    triggered = False
    mode = "none"

    # ═══════════════════════════════════════
    # Layer 1: Mode C — 超热门翻车风险
    # v5.3: 不改判, 仅 risk_tag + 阈值降低 + 置信度衰减
    # ═══════════════════════════════════════
    if max_imp >= mcc.get("imp_min", 0.72):
        # 额外: 平赔 <6.0 = 庄家异常信号
        if od <= mcc.get("od_max", 6.0):
            result["risk_tag"] = "upset_warning"
            result["draw_threshold_adj"] = max(0.18, 0.32 - mcc.get("draw_threshold_drop", 0.10))
            result["confidence_mult"] = mcc.get("confidence_decay", 0.85)
            result["draw_boost"] = mcc.get("draw_boost", 0.06)
            result["dgate_mode"] = "C"
            result["triggered_signals"].append(f"mode_c(imp={max_imp:.0%}, od={od:.2f})")
            triggered = True
            mode = "C"

    # ═══════════════════════════════════════
    # Layer 1b: Mode C-away
    # v5.3: 客队热门翻车风险
    # ═══════════════════════════════════════
    if not triggered and mca and imp_a >= mca.get("pa_min", 0.65) and max_imp < mcc.get("imp_min", 0.72):
        result["risk_tag"] = "upset_warning"
        result["draw_threshold_adj"] = max(0.20, 0.32 - mca.get("draw_threshold_drop", 0.08))
        result["confidence_mult"] = mca.get("confidence_decay", 0.88)
        result["draw_boost"] = mca.get("draw_boost", 0.04)
        result["dgate_mode"] = "C-away"
        result["triggered_signals"].append(f"mode_c_away(pa={imp_a:.0%})")
        triggered = True
        mode = "C-away"

    # ═══════════════════════════════════════
    # Layer 2: Mode A — 中等热门 (画局风险)
    # v5.3: DrawExpert boost + spread权衡
    # ═══════════════════════════════════════
    if not triggered and ma.get("imp_min", 0.38) <= max_imp <= ma.get("imp_max", 0.70):
        boost = float(imp_d) * 1.08
        suppress = max(0.75, 1.0 - spread * ma.get("spread_power", 0.25))
        boost *= suppress

        if ou <= 2.5:
            boost *= ma.get("ou_boost", 1.03)

        # S7+S1 屠杀预警
        if s7_ou_hcp_ratio >= ma.get("s7_threshold", 3.5) and s1_draw_cheapness < ma.get("s1_threshold", 1.25):
            boost *= ma.get("s7s1_penalty", 0.65)

        # v5.3: DrawExpert 信号增强
        if draw_expert_signal is not None and draw_expert_signal > 0.30:
            boost *= min(1.35, 1.0 + draw_expert_signal * 0.5)

        # v5.3+: λ残差辅助
        if lambda_residual is not None and lambda_residual > 0.3:
            boost *= 1.15

        threshold = ma.get("threshold", 0.26)
        if boost > threshold:
            result["risk_tag"] = "draw_alert"
            result["draw_threshold_adj"] = max(0.22, 0.32 - ma.get("draw_threshold_drop", 0.05))
            result["confidence_mult"] = ma.get("confidence_decay", 0.92)
            result["draw_boost"] = ma.get("draw_boost", 0.04) + (draw_expert_signal or 0) * 0.15
            result["dgate_mode"] = "A"
            result["triggered_signals"].append(f"mode_a(imp={max_imp:.0%}, spread={spread:.3f})")
            triggered = True
            mode = "A"

    # ═══════════════════════════════════════
    # Layer 3: Mode B — 均衡赛
    # v5.3: spread阈值分杯赛/联赛
    # ═══════════════════════════════════════
    if not triggered and spread < mb.get("spread_max", 0.22):
        boost = float(imp_d) * 1.08 * mb.get("boost", 1.15)

        # v5.3: DrawExpert 信号增强
        if draw_expert_signal is not None and draw_expert_signal > 0.28:
            boost *= min(1.25, 1.0 + draw_expert_signal * 0.4)

        threshold = mb.get("threshold", 0.40)
        if boost > threshold:
            result["risk_tag"] = "draw_alert"
            result["draw_threshold_adj"] = max(0.24, 0.32 - mb.get("draw_threshold_drop", 0.04))
            result["confidence_mult"] = mb.get("confidence_decay", 0.95)
            result["draw_boost"] = mb.get("draw_boost", 0.03) + (draw_expert_signal or 0) * 0.10
            result["dgate_mode"] = "B"
            result["triggered_signals"].append(f"mode_b(spread={spread:.3f})")
            triggered = True
            mode = "B"

    # ═══ v5.3+: 赔率斜率触发 (实时信号) ═══
    if not triggered and odds_slope is not None and abs(odds_slope) > 0.08:
        result["risk_tag"] = "draw_alert"
        result["draw_threshold_adj"] = 0.26
        result["confidence_mult"] = 0.90
        result["draw_boost"] = 0.03
        result["dgate_mode"] = "slope"
        result["triggered_signals"].append(f"odds_slope({odds_slope:+.3f})")

    return result


# ═══════════════════════════════════════════════════════════
# 便捷函数: 从赔率直接计算全套
# ═══════════════════════════════════════════════════════════

def imp_from_odds(oh: float, od: float, oa: float) -> Tuple[float, float, float]:
    """赔率 → 隐含概率 (去抽水)"""
    s = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/s, (1.0/od)/s, (1.0/oa)/s


def quick_scan(
    oh: float, od: float, oa: float,
    league: str = "",
    handicap: float = 0.0,
    ou_line: float = 2.5,
    de_signal: Optional[float] = None,
) -> Dict[str, Any]:
    """一键扫描: 赔率 → DrawGate 结果"""
    imp_h, imp_d, imp_a = imp_from_odds(oh, od, oa)
    mtype = detect_match_type(league)
    return apply_drawgate(
        imp_h, imp_d, imp_a,
        odds={"home": oh, "draw": od, "away": oa},
        handicap=handicap, ou_line=ou_line,
        match_type=mtype,
        draw_expert_signal=de_signal,
    )
