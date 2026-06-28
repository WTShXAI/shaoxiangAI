"""
D-Gate 统一引擎 — P0-5修复: 全系统统一使用 DrawGate v5.3
==========================================================
v5.1→v5.3 升级 (2026-06-26):
  - 底层引擎: drawgate_v53.apply_drawgate() (Mode C/C-away/A + away_skepticism + group_stage_rotation)
  - 兼容层: apply_dgate_v51/apply_dgate 保持 v4.9 API 签名不变
  - 新增字段: risk_tag, draw_threshold_adj, confidence_mult 透传 v5.3
  - detect_match_type 仍独立维护 (与 v5.3 保持同步)

2026-06-28: 工具函数迁移至 d_gate_utils.py (原 d_gate_v52.py)
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
# P0-5: v5.1→v5.3 适配器 — 底层使用 DrawGate v5.3
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
    """D-Gate 统一接口 — 底层 DrawGate v5.3 (P0-5)

    保持 v4.9/v5.1 API 签名不变, 内部委托至 drawgate_v53.apply_drawgate().
    返回字段向下兼容: d_gate_active, verdict, d_boosted, d_gate_mode, signals.
    新增 v5.3 字段: risk_tag, draw_threshold_adj, confidence_mult.
    """
    from rules.drawgate_v53 import apply_drawgate, detect_match_type as v53_detect

    h_use = h_adj if h_adj is not None else imp_h
    a_use = a_adj if a_adj is not None else imp_a
    d_use = d_boosted if d_boosted is not None else imp_d

    mtype = v53_detect(match_type) if match_type else "tournament"

    dg = apply_drawgate(
        imp_h, imp_d, imp_a,
        odds=odds,
        handicap=handicap, ou_line=ou_line,
        match_type=mtype,
    )

    # ── 映射 v5.3 → v5.1 兼容字段 ──
    d_gate_active = dg['risk_tag'] != 'clean'

    # verdict: v5.3 不直接输出, 从上下文推断
    if d_gate_active and dg['dgate_mode'] != 'none':
        verdict = 'D'
    elif h_use >= a_use:
        verdict = 'H'
    else:
        verdict = 'A'

    d_boosted_out = min(d_use + dg.get('draw_boost', 0), 0.55)

    # score_predictions 兼容 (v5.1 特有, v5.3不处理)
    score_contradiction = 0
    if score_predictions:
        predicted_types = []
        for s in score_predictions[:6]:
            try:
                if isinstance(s, dict):
                    hg, ag = s.get('home_goals', 0), s.get('away_goals', 0)
                elif isinstance(s, (list, tuple)) and len(s) >= 2:
                    hg, ag = s[0], s[1]
                else:
                    continue
                if hg == ag:
                    predicted_types.append('D')
                elif hg > ag:
                    predicted_types.append('H')
                else:
                    predicted_types.append('A')
            except Exception:
                continue
        score_contradiction = len([t for t in predicted_types if t == 'D'])

    return {
        # v5.1 兼容字段
        "d_gate_active": d_gate_active,
        "d_gate_mode": dg['dgate_mode'],
        "verdict": verdict,
        "d_boosted": d_boosted_out,
        "signals": dg['triggered_signals'],
        # v5.1 extras (保留兼容)
        "s1_draw_cheapness": 0.0,
        "s7_ou_hcp_ratio": 0.0,
        # v5.3 新增字段 (caller可选使用)
        "risk_tag": dg['risk_tag'],
        "draw_threshold_adj": dg['draw_threshold_adj'],
        "confidence_mult": dg['confidence_mult'],
        "draw_boost": dg['draw_boost'],
        # score_predictions 兼容
        "score_contradiction": score_contradiction,
    }

# ═══════════════════════════════════════════════════════════
# 兼容接口: 保持 v4.9 API 不变 → 路由到 v5.3
# ═══════════════════════════════════════════════════════════

def apply_dgate(*args, **kwargs) -> Dict[str, Any]:
    """向后兼容接口 — P0-5: 路由到 DrawGate v5.3"""
    return apply_dgate_v51(*args, **kwargs)
