"""
D-Gate v5.1 统一引擎 — 多信号分层判型 + 赔率深层信号
========================================================
升级自 v4.9 (P3落地版):
  v5.0: Mode C 反转(超热门spread大=翻车信号, ×2.2)
  v5.1: S7+S1赔率深层过滤 + Mode C-away + 分层阈值
"""
import json, math, logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

_RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "tournament_rules.json"
_RULES_CACHE: Optional[Dict] = None


def _load_rules() -> Dict:
    global _RULES_CACHE
    if _RULES_CACHE is not None:
        return _RULES_CACHE
    try:
        with open(_RULES_PATH, 'r', encoding='utf-8') as f:
            _RULES_CACHE = json.load(f)
        logger.info(f"[D-Gate v5.1] 赛事规则加载成功")
        return _RULES_CACHE
    except Exception as e:
        logger.warning(f"[D-Gate v5.1] 规则加载失败, 使用默认: {e}")
        _RULES_CACHE = _default_rules()
        return _RULES_CACHE


def _default_rules() -> Dict:
    return {
        "tournament": {
            "dgate": {
                "mode_c": {"imp_min": 0.70, "boost_tier1": 2.2, "boost_tier2": 1.8,
                          "tier1_imp": 0.75, "tier1_hcp": 1.75, "threshold": 0.14,
                          "anti_od": 9.5, "anti_ou": 3.5, "anti_hcp": 2.5},
                "mode_c_away": {"pa_min": 0.65, "boost": 2.0, "threshold": 0.14},
                "mode_a": {"imp_min": 0.48, "imp_max": 0.70, "threshold": 0.28,
                          "spread_power": 0.30, "ou_boost": 1.05,
                          "s7_threshold": 3.5, "s1_threshold": 1.30, "s7s1_penalty": 0.70},
                "mode_b": {"spread_max": 0.15, "boost": 1.20, "threshold": 0.43},
                "default": {"threshold": 0.32, "s7_threshold": 3.5, "s1_threshold": 1.30, "s7s1_penalty": 0.70},
            }
        },
        "league": {
            "dgate": {
                "mode_c": {"imp_min": 0.72, "boost_tier1": 1.8, "boost_tier2": 1.5,
                          "tier1_imp": 0.78, "tier1_hcp": 1.75, "threshold": 0.18,
                          "anti_od": 9.5, "anti_ou": 3.5, "anti_hcp": 2.5},
                "mode_a": {"imp_min": 0.48, "imp_max": 0.68, "threshold": 0.32,
                          "spread_power": 0.35, "ou_boost": 1.03,
                          "s7_threshold": 3.5, "s1_threshold": 1.25, "s7s1_penalty": 0.65},
                "mode_b": {"spread_max": 0.12, "boost": 1.15, "threshold": 0.45},
                "default": {"threshold": 0.34, "s7_threshold": 4.0, "s1_threshold": 1.20, "s7s1_penalty": 0.60},
            }
        },
    }


# ── 赛事类型检测 ──
_TOURNAMENT_KEYWORDS = [
    "世界杯", "world cup", "欧洲杯", "euro", "美洲杯", "copa america",
    "亚洲杯", "asian cup", "非洲杯", "afcon", "欧冠", "champions league",
    "欧联", "europa league", "杯赛", "锦标赛", "淘汰赛", "小组赛",
    "round of", "group", "knockout", "final", "semi",
]
_LEAGUE_KEYWORDS = [
    "英超", "premier league", "西甲", "la liga", "意甲", "serie a",
    "德甲", "bundesliga", "法甲", "ligue 1", "中超", "联赛", "league",
    "英冠", "championship", "荷甲", "eredivisie", "葡超",
]


def detect_match_type(text: str = "") -> str:
    text_lower = text.lower()
    for kw in _TOURNAMENT_KEYWORDS:
        if kw in text_lower:
            return "tournament"
    for kw in _LEAGUE_KEYWORDS:
        if kw in text_lower:
            return "league"
    return "tournament"


# ═══════════════════════════════════════════════════════════
# D-Gate v5.1 核心: 多信号分层判型
# ═══════════════════════════════════════════════════════════

def apply_dgate_v51(
    imp_h: float,
    imp_d: float,
    imp_a: float,
    odds: Dict[str, float],
    handicap: Optional[float] = None,
    ou_line: Optional[float] = None,
    water_level: Optional[float] = None,
    fifa_rank_diff: Optional[int] = None,
    group_round: Optional[int] = None,
    match_type: str = "tournament",
    h_adj: Optional[float] = None,
    a_adj: Optional[float] = None,
    d_boosted: Optional[float] = None,
    score_predictions: Optional[list] = None,
) -> Dict[str, Any]:
    """D-Gate v5.1 — 赔率深层信号增强判型

    五层架构: Mode C → Mode C-away → Mode A → Mode B → Default
    核心创新: Mode C spread反转 + S7/S1赔率深层信号
    """
    if h_adj is None: h_adj = imp_h
    if a_adj is None: a_adj = imp_a
    if d_boosted is None: d_boosted = imp_d

    oh = odds.get('home', 2.0)
    od = odds.get('draw', 3.2)
    oa = odds.get('away', 2.0)
    spread = abs(imp_h - imp_a)
    max_imp = max(imp_h, imp_a)
    hcp = handicap or 0.0
    ou = ou_line or 2.5

    # ═══ v5.1 赔率深层信号 ═══
    s1_draw_cheapness = od / math.sqrt(oh * oa) if oh > 0 and oa > 0 else 1.0
    s7_ou_hcp_ratio = ou / max(abs(hcp), 0.25)

    # ── 加载配置 ──
    rules = _load_rules()
    cfg = rules.get(match_type, rules.get("tournament", {})).get("dgate", {})
    mcc = cfg.get("mode_c", {})
    mca = cfg.get("mode_c_away", {})
    ma = cfg.get("mode_a", {})
    mb = cfg.get("mode_b", {})
    md = cfg.get("default", {})

    triggered_signals = []
    d_gate_active = False
    d_gate_mode = ""

    # ═══════════════════════════════════════
    # Layer 1: Mode C — 超热门翻车
    # 核心创新: spread大≠抑制平局, 而是翻车信号(×boost)
    # ═══════════════════════════════════════
    if max_imp >= mcc.get("imp_min", 0.70):
        boost = pd = float(d_boosted)
        boost *= 1.08  # WC全局

        if max_imp > mcc.get("tier1_imp", 0.75) or abs(hcp) >= mcc.get("tier1_hcp", 1.75):
            boost *= mcc.get("boost_tier1", 2.2)
        else:
            boost *= mcc.get("boost_tier2", 1.8)

        # 反过滤: 极度安全结构
        if od > mcc.get("anti_od", 9.5) and ou >= mcc.get("anti_ou", 3.5) and abs(hcp) >= mcc.get("anti_hcp", 2.5):
            boost *= 0.3
        elif od > mcc.get("anti_od", 9.5) and abs(hcp) >= mcc.get("anti_hcp", 2.5):
            boost *= 0.5

        threshold = mcc.get("threshold", 0.14)
        if boost > threshold:
            d_gate_active = True
            d_gate_mode = "C"
            d_boosted = min(boost, 0.55)
            triggered_signals.append(f"mode_c(max_imp={max_imp:.0%})")
            logger.info(f"[D-Gate v5.1] Mode C: max_imp={max_imp:.0%} boost={boost:.3f} > {threshold}")

    # ═══════════════════════════════════════
    # Layer 1b: Mode C-away — 客场强队
    # ═══════════════════════════════════════
    if not d_gate_active and mca and imp_a > mca.get("pa_min", 0.65) and max_imp < mcc.get("imp_min", 0.70):
        boost = float(d_boosted) * 1.08 * mca.get("boost", 2.0)
        threshold = mca.get("threshold", 0.14)
        if boost > threshold:
            d_gate_active = True
            d_gate_mode = "C-away"
            d_boosted = min(boost, 0.50)
            triggered_signals.append(f"mode_c_away(pa={imp_a:.0%})")
            logger.info(f"[D-Gate v5.1] Mode C-away: pa={imp_a:.0%} boost={boost:.3f}")

    # ═══════════════════════════════════════
    # Layer 2: Mode A — 中等热门
    # ═══════════════════════════════════════
    if not d_gate_active and ma.get("imp_min", 0.48) <= max_imp <= ma.get("imp_max", 0.70):
        boost = float(d_boosted) * 1.08
        suppress = max(0.80, 1.0 - spread * ma.get("spread_power", 0.30))
        boost *= suppress

        if ou <= 2.5:
            boost *= ma.get("ou_boost", 1.05)

        # S7+S1: OU/HCP比高 + draw赔率贵 = 屠杀预警
        if s7_ou_hcp_ratio >= ma.get("s7_threshold", 3.5) and s1_draw_cheapness < ma.get("s1_threshold", 1.30):
            boost *= ma.get("s7s1_penalty", 0.70)

        threshold = ma.get("threshold", 0.28)
        if boost > threshold:
            d_gate_active = True
            d_gate_mode = "A"
            d_boosted = min(boost, 0.45)
            triggered_signals.append(f"mode_a(max_imp={max_imp:.0%})")
            logger.info(f"[D-Gate v5.1] Mode A: max_imp={max_imp:.0%} boost={boost:.3f} > {threshold}")

    # ═══════════════════════════════════════
    # Layer 3: Mode B — 均衡赛 (高门槛)
    # ═══════════════════════════════════════
    if not d_gate_active and spread < mb.get("spread_max", 0.15):
        boost = float(d_boosted) * 1.08 * mb.get("boost", 1.20)
        threshold = mb.get("threshold", 0.43)
        if boost > threshold:
            d_gate_active = True
            d_gate_mode = "B"
            d_boosted = min(boost, 0.45)
            triggered_signals.append(f"mode_b(spread={spread:.3f})")

    # ═══════════════════════════════════════
    # Layer 4: Default
    # ═══════════════════════════════════════
    verdict = ""
    if not d_gate_active:
        boost = float(d_boosted) * 1.08
        if spread > 0.40:
            boost *= 0.70
        elif spread > 0.20:
            boost *= 0.85

        if s7_ou_hcp_ratio >= md.get("s7_threshold", 3.5) and s1_draw_cheapness < md.get("s1_threshold", 1.30):
            boost *= md.get("s7s1_penalty", 0.70)

        threshold = md.get("threshold", 0.32)
        if boost > threshold:
            verdict = "D"
            d_gate_mode = "default"
            d_boosted = boost
            triggered_signals.append("dgate_default")
        elif h_adj >= a_adj:
            verdict = "H"
        else:
            verdict = "A"
    else:
        verdict = "D"

    return {
        "d_gate_active": d_gate_active,
        "d_gate_mode": d_gate_mode,
        "verdict": verdict,
        "d_boosted": d_boosted,
        "signals": triggered_signals,
        # v5.1 extras
        "s1_draw_cheapness": s1_draw_cheapness,
        "s7_ou_hcp_ratio": s7_ou_hcp_ratio,
    }


# ═══════════════════════════════════════════════════════════
# 兼容接口: 保持 v4.9 API 不变
# ═══════════════════════════════════════════════════════════

def apply_dgate(*args, **kwargs) -> Dict[str, Any]:
    """向后兼容接口 — 自动路由到 v5.1"""
    return apply_dgate_v51(*args, **kwargs)
