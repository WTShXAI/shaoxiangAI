# -*- coding: utf-8 -*-
"""
pipeline.predictors.model_router — 匹配模型路由分发器 (SSoT 对齐层)

职责
────
用户指令: "把**所有模型的分析结果向匹配的模型对齐**".
本模块是唯一的对外对齐入口, 所有分析请求 (决策智能体卡 / 前端 / API) 都必须经它,
禁平行模型各自为政.

设计原则 (沿用 model_catalog SSoT, 禁平行重造):
- 模型路由是**任务制** (M1-M7), 不是赛事制. WC/League 专用引擎在 catalog 中已标记
  DEPRECATED 死链, 不应再按 cup/league 分流 (那是反模式).
- "匹配模型" = model_catalog 中覆盖该任务的活跃模型, 由 ranked_predictor 统一编排
  (WI教师主导0.85 + 庄家去水锚 + fl结构信号 + jepa交叉校验 + 独立残差).
- 多模型分歧由 ConsistencyValidator.reconcile_components 校准: 主模型(融合结果)胜出,
  冲突仅作透明标注, 不覆盖结论.

用法
────
  from pipeline.predictors.model_router import ModelRouter
  res = ModelRouter.analyze(home, away, h, d, a, ou_line=2.5, ou_over=1.9, ...)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _safe_ranked_predict(home, away, h, d, a, **kw) -> Optional[Dict[str, Any]]:
    """延迟调用 ranked_predictor.predict, 失败返回 None (上游降级到诚实锚)."""
    try:
        from pipeline.ranked_predictor import predict as _rp
        return _rp(home, away, h, d, a, **kw)
    except Exception as e:
        logger.warning("[ModelRouter] ranked_predictor 调用失败, 降级: %s", e)
        return None


class ModelRouter:
    """匹配模型分发器 — 所有分析请求的唯一对齐入口."""

    # 组件标签 → 中文名 (用于分歧透明度展示)
    _COMP_LABELS = {
        "wi_teacher": "WI教师(主导)",
        "devig_raw": "庄家去水(锚)",
        "jepa_dc": "JEPA交叉校验",
        "fl_structure": "FL结构信号",
        "independent": "独立残差模型",
        "analysis_center": "历史相似频率",
    }

    @classmethod
    def analyze(
        cls,
        home: str,
        away: str,
        h: float,
        d: float,
        a: float,
        ou_line: Optional[float] = None,
        ou_over: Optional[float] = None,
        ou_under: Optional[float] = None,
        op_cs: Optional[Any] = None,
        ah_line: Optional[float] = None,
        ah_home: Optional[float] = None,
        ah_away: Optional[float] = None,
        league: Optional[str] = None,
        inplay: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """返回对齐后的单一分析结论.

        Returns:
            {
              "matched_model": "ranked_predictor (M1-M7 任务制编排)",
              "aligned": True,
              "verdict_1x2": "主胜"/"平局"/"客胜",
              "probs_1x2": {"主胜":p_h,"平局":p_d,"客胜":p_a},
              "expected_total": float|None,
              "ou": {"line","direction","p_over","p_under"},
              "cs_top": [("2-1",p), ...],
              "ah": {...}|None,
              "confidence_tier": str,
              "analysis": <ranked_predictor 结构化分析 dict>,
              "reconcile": {  # 多模型分歧校准
                  "conflict": bool,
                  "spread": float,           # 各组件 top-label 概率极差
                  "components": {...},       # 各组件 1X2 概率 (透明)
                  "note": str,
              },
              "operator_intent": ...,
              "fallback": False,            # True 表示 ranked 失败, 用诚实锚降级
            }
        """
        rp = _safe_ranked_predict(
            home, away, h, d, a,
            ou_line=ou_line, ou_over=ou_over, ou_under=ou_under,
            op_cs=op_cs, ah_line=ah_line, ah_home=ah_home, ah_away=ah_away,
            league=league, inplay=inplay,
        )
        if rp is None:
            return cls._fallback(home, away, h, d, a, ou_line, ou_over, ou_under)

        # ── 多模型分歧校准 ──
        try:
            from pipeline.predictors.consistent_validator import ConsistencyValidator
            reconcile = ConsistencyValidator.reconcile_components(rp)
        except Exception as e:
            logger.warning("[ModelRouter] reconcile 失败, 跳过: %s", e)
            reconcile = {"conflict": False, "spread": 0.0,
                         "components": {}, "note": "校准层不可用"}

        m1x2 = rp.get("markets", {}).get("1x2", {})
        ranked = m1x2.get("ranked", [])
        verdict = ranked[0][0] if ranked else "?"
        probs = {
            "主胜": m1x2.get("p_h"),
            "平局": m1x2.get("p_d"),
            "客胜": m1x2.get("p_a"),
        }
        ou = rp.get("markets", {}).get("ou", {})
        cs = rp.get("markets", {}).get("cs", {})
        ah = rp.get("markets", {}).get("ah")

        return {
            "matched_model": "ranked_predictor (M1-M7 任务制编排)",
            "aligned": True,
            "verdict_1x2": verdict,
            "probs_1x2": probs,
            "expected_total": m1x2.get("expected_total"),
            "ou": {
                "line": ou.get("line"),
                "direction": ou.get("direction"),
                "p_over": ou.get("p_over"),
                "p_under": ou.get("p_under"),
            },
            "cs_top": cs.get("ranked", [])[:3],
            "ah": ah,
            "confidence_tier": rp.get("confidence_tier"),
            "analysis": rp.get("analysis"),
            "reconcile": reconcile,
            "operator_intent": rp.get("operator_intent"),
            "operator_verdict": rp.get("operator_verdict"),
            "fallback": False,
        }

    # ───────────────────────────────────────────────────────────
    #  降级: ranked 失败时退回诚实锚 (1X2 受注位 + OU 隐含总球)
    # ───────────────────────────────────────────────────────────
    @classmethod
    def _fallback(cls, home, away, h, d, a, ou_line, ou_over, ou_under) -> Dict[str, Any]:
        inv = [1 / x for x in (h, d, a)]
        z = sum(inv)
        p_h, p_d, p_a = [v / z for v in inv]
        ranked = sorted([("主胜", p_h), ("平局", p_d), ("客胜", p_a)], key=lambda x: -x[1])
        total = None
        if ou_line and ou_over and ou_under:
            # 对称去水: 低水方向 = 庄家护的聪明边
            if ou_over < ou_under:
                total = ou_line + 0.5
            else:
                total = ou_line - 0.5
        return {
            "matched_model": "诚实锚 (ranked_predictor 不可用, 降级)",
            "aligned": True,
            "verdict_1x2": ranked[0][0],
            "probs_1x2": {"主胜": round(p_h, 4), "平局": round(p_d, 4), "客胜": round(p_a, 4)},
            "expected_total": total,
            "ou": {"line": ou_line, "direction": None,
                   "p_over": ou_over, "p_under": ou_under},
            "cs_top": [],
            "ah": None,
            "confidence_tier": "低",
            "analysis": None,
            "reconcile": {"conflict": False, "spread": 0.0, "components": {},
                         "note": "主模型不可用, 已降级至诚实锚"},
            "operator_intent": None,
            "operator_verdict": None,
            "fallback": True,
        }
