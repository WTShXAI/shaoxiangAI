"""
足球领域知识修正引擎 (Domain Knowledge Engine)
=================================================
从 rules/football_kb.yaml 读取领域知识，修正 ML 模型输出的基础概率。

核心规则:
    1. 德比检测      → 平局概率微升 (+0.03)，主/客各降 0.015
    2. Top6 主场加成 → 主胜概率 × home_advantage_factor
    3. 关键伤停惩罚  → 主胜概率 -= Σ(penalty_per_position)

约束:
    - 禁止修改 EnsembleTrainer 本身
    - football_kb.yaml 缺失 → FileNotFoundError（Fail-Fast）
    - 最终输出强制归一化（H+D+A = 1.0）
    - 兼容 Windows 路径

from utils.constants import DEFAULT_DRAW_PROB

用法:
    from rules.domain_rules import apply_domain_knowledge
    adjusted = apply_domain_knowledge(features, base_proba)
"""

import os
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# ── 缓存 ──────────────────────────────────────────────
_KB_CACHE: Optional[Dict[str, Any]] = None

def _load_kb() -> Dict[str, Any]:
    """加载足球领域知识库（带缓存，幂等）"""
    global _KB_CACHE
    if _KB_CACHE is not None:
        return _KB_CACHE

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kb_path = os.path.join(project_root, "rules", "football_kb.yaml")

    if not os.path.exists(kb_path):
        raise FileNotFoundError(
            f"[DomainKB] 🚫 足球领域知识库缺失: {kb_path}\n"
            "请确保 rules/football_kb.yaml 存在。"
            "该文件是预测修正的必要输入，禁止静默跳过。"
        )

    try:
        import yaml
        with open(kb_path, "r", encoding="utf-8") as f:
            _KB_CACHE = yaml.safe_load(f)
    except (Exception, KeyError, IndexError, IOError, FileNotFoundError) as e:
        raise RuntimeError(f"[DomainKB] 知识库解析失败: {e}") from e

    logger.info(f"[DomainKB] ✅ 知识库已加载: {kb_path}")
    return _KB_CACHE

def apply_domain_knowledge(
    features: Dict[str, Any],
    base_proba: Dict[str, float],
) -> Dict[str, Any]:
    """
    应用足球领域知识修正 ML 模型输出的基础概率。

    Args:
        features: 特征字典，应包含:
            - home_team_name / home_team: 主队名称
            - away_team_name / away_team: 客队名称
            - is_home: 是否为主场（默认 True）
            - injured_positions: 伤停位置列表，如 ["GK", "ST"]
        base_proba: ML 模型原始概率，如 {"home": 0.45, "draw": 0.28, "away": 0.27}

    Returns:
        {
            "home": float,
            "draw": float,
            "away": float,
            "_kb_applied": ["derby_boost", "top6_home_advantage", "injury_penalty_GK"],
            "_kb_raw_ml": {"home": float, "draw": float, "away": float},
        }
    """
    kb = _load_kb()
    teams = kb.get("teams", {})
    derbies = kb.get("derbies", [])
    injuries_cfg = kb.get("injuries", {})

    applied_rules: List[str] = []

    # ── 提取队伍名称 ──────────────────────────────────
    home_team = (
        features.get("home_team_name")
        or features.get("home_team")
        or ""
    )
    away_team = (
        features.get("away_team_name")
        or features.get("away_team")
        or ""
    )

    # ── 提取原始概率 ──────────────────────────────────
    h = float(base_proba.get("home", 0.33))
    d = float(base_proba.get("draw", DEFAULT_DRAW_PROB))
    a = float(base_proba.get("away", 0.33))
    raw_ml = {"home": round(h, 4), "draw": round(d, 4), "away": round(a, 4)}

    # ── 规则 1: 德比检测 ──────────────────────────────
    if home_team and away_team:
        for pair in derbies:
            if sorted([home_team, away_team]) == sorted(pair):
                d += 0.03
                h -= 0.015
                a -= 0.015
                applied_rules.append("derby_boost")
                logger.info(
                    f"[DomainKB] 德比 detected: {home_team} vs {away_team}"
                )
                break

    # ── 规则 2: Top6 主场优势 ─────────────────────────
    home_info = teams.get(home_team, {})
    if home_info.get("is_top6") and features.get("is_home", True):
        factor = float(home_info.get("home_advantage_factor", 1.0))
        h *= factor
        applied_rules.append("top6_home_advantage")
        logger.info(
            f"[DomainKB] Top6 主场加成: {home_team} factor={factor:.3f}"
        )

    # ── 规则 3: 关键球员伤停 ──────────────────────────
    injured_positions = features.get("injured_positions", [])
    if injured_positions and isinstance(injured_positions, list):
        key_positions = injuries_cfg.get("key_positions", [])
        penalties = injuries_cfg.get("penalty_per_position", {})
        total_penalty = 0.0
        for pos in injured_positions:
            if pos in key_positions:
                p = float(penalties.get(pos, 0.05))
                total_penalty += p
                applied_rules.append(f"injury_penalty_{pos}")
        if total_penalty > 0:
            h -= total_penalty
            applied_rules.append("injury_penalty_total")
            logger.info(
                f"[DomainKB] 伤停惩罚: {home_team} "
                f"positions={injured_positions} penalty={total_penalty:.3f}"
            )

    # ── 强制归一化 ────────────────────────────────────
    total = h + d + a
    if total > 0:
        h = h / total
        d = d / total
        a = a / total
    else:
        h, d, a = 0.33, 0.34, 0.33
        logger.warning("[DomainKB] 概率归一化分母为0，回退到均匀分布")

    return {
        "home": round(h, 4),
        "draw": round(d, 4),
        "away": round(a, 4),
        "_kb_applied": applied_rules,
        "_kb_raw_ml": raw_ml,
    }
