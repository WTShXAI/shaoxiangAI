# -*- coding: utf-8 -*-
"""
pipeline.model_catalog — 模型资产单一真相源 (SSoT, 2026-08-05 收敛)

背景
────
2026-08-05 盘点发现项目累积 56 个权重文件 / 17+ 个概率产出模块, 其中:
  · 线上活跃权重仅 8 个
  · 30 个权重零引用 (134.6 MB 死重)
  · model_dispatcher / pattern_matcher / model_registry 整条链仅被 tmp/ 临时脚本引用 = 死链
  · WC/League 引擎 (pipeline.engine) 前端零调用, 仅遗留 /predict 端点保活

用户指令: 模型收敛到 7 个以内.

本模块定义**唯一合法的 7 个模型 (M1-M7)**. 任何新增模型必须先在此登记,
且总数不得超过 7 —— `validate()` 会在超标时抛错.

模型 vs 基础设施 的界定
──────────────────────
**是模型**: 独立产出概率 / 信号 / 排序分数, 可被单独评估 (AUC / ROI).
**非模型**(不计入 7): 编排器(ranked_predictor)、融合器(model_ensemble)、
  校准层(calibration_overlay / cs_calibration)、特征库(feature_library)、
  去水工具(opening_line / clean_outcomes)、采集与存储.

前端实际调用的端点 (2026-08-05 核实)
───────────────────────────────────
  /api/predict/ranked        → ranked_predictor 编排 → M1 M2 M3 M5 M6 M7
  /api/analysis/scan         → M7
  /api/template-deviation    → M7
  /api/leyu/value-signal     → M6
  /api/terminal/analyze      → M3 M4
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

MAX_MODELS = 7


@dataclass(frozen=True)
class ModelSpec:
    mid: str                       # M1..M7
    name: str                      # 中文名
    role: str                      # 职责 (一句话)
    module: str                    # 实现模块 (SSoT, 禁平行重造)
    weights: List[str] = field(default_factory=list)   # 权重文件 (相对项目根)
    tasks: List[str] = field(default_factory=list)     # 覆盖任务
    status: str = "active"         # active | degraded | shadow
    note: str = ""


# ═══════════════════════════════════════════════════════════════
#  M1-M7 —— 唯一合法模型集
# ═══════════════════════════════════════════════════════════════
CATALOG: Dict[str, ModelSpec] = {
    "M1": ModelSpec(
        mid="M1",
        name="WI 主模型",
        role="1X2 与总进球主导概率 (威廉+Inter 真实特征训练的教师模型)",
        module="pipeline.william_inter_model",
        weights=["data/wi_1x2_model.joblib", "data/wi_total_model.joblib"],
        tasks=["1X2", "TOTAL"],
        status="active",
        note="ranked_predictor 中权重 0.85, 另 0.15 为庄家去水锚定地板(盘口锚定铁律).",
    ),
    "M2": ModelSpec(
        mid="M2",
        name="Score 比分模型",
        role="Poisson/Dixon-Coles 比分分布, 派生 CS / OU / 总进球",
        module="pipeline.score_model",
        weights=["saved_models/dc_score_model.joblib"],
        tasks=["CS", "OU", "TOTAL"],
        status="active",
        note="CS 以市场 CS 赔率为主(93.2% 覆盖), 泊松仅回退. "
             "半场条件大小球(inplay_ou)作为本模型的 in-play 分支, 不单列.",
    ),
    "M3": ModelSpec(
        mid="M3",
        name="ReverseOdds 赔率破解",
        role="庄家意图分类 / 陷阱检测 / 误定价评分 / 凯利注码",
        module="pipeline.reverse_odds_engine",
        weights=["saved_models/mispricing_detector.joblib"],
        tasks=["INTENT", "MISPRICING"],
        status="active",
        note="产出附于概率排名作意图校验, 不覆盖概率.",
    ),
    "M4": ModelSpec(
        mid="M4",
        name="OperatorSignal 操盘手信号",
        role="开盘→收盘漂移的逆转检测与漂移可靠度评分",
        module="pipeline.operator_signals",
        weights=[
            "saved_models/operator_reversal_detector.joblib",
            "saved_models/operator_drift_reliability.joblib",
        ],
        tasks=["REVERSAL", "DRIFT"],
        status="active",
        note="零漂移(drift=0)时禁止输出方向信号 —— 见 decode-consistency-audit 铁律.",
    ),
    "M5": ModelSpec(
        mid="M5",
        name="FL 结构库模型",
        role="GQ 单庄赔率结构树模型 (纯赔率结构推导, 不依赖球队特征)",
        module="pipeline.fl_predictor",
        weights=["data/fl_model_1x2.joblib"],
        tasks=["1X2"],
        status="active",
        note="✅ 2026-08-08 修复: bridge_service 推理端改用 fl_predictor 真实37维特征 "
             "(原22维硬编码伪造特征已移除, dt_vote 不再恒 None). 三任务 fl_model_{1x2,ou,ah} 均已"
             "重训上线, AH 复活(1347样本, +15pp). 默认 fl_structure_weight=0.0 仅透明展示不融合.",
    ),
    "M6": ModelSpec(
        mid="M6",
        name="ValueSignal 价值信号",
        role="跨庄/跨市场软线价差 → 真 +EV 识别",
        module="pipeline.leyu_value_signal",
        weights=[],
        tasks=["VALUE", "EV"],
        status="degraded",
        note="数学铁律: +EV ⇔ sharp_prob × odds − 1 > 0 ⇔ divergence > book_margin. "
             "乐鱼(GQ)为纯单庄, 无第二家盘口 ⇒ 当前分歧恒 0, 如实 PASS. "
             "尖庄钩子 SHARP_CONSENSUS_PROVIDER 预留待接. "
             "并入 cross_book_edge / multibook_consensus 两个同源子模块.",
    ),
    "M7": ModelSpec(
        mid="M7",
        name="Neighbor 历史相似统计",
        role="相似盘口结构的历史邻居频率 + 定价模板偏离度",
        module="pipeline.analysis_center",
        weights=[],
        tasks=["1X2_PRIOR", "TEMPLATE_RISK"],
        status="active",
        note="扫盘榜单 + 模板弱区标签同源(都是历史相似结构统计), 合并为一个模型. "
             "在 ranked_predictor 中作 blend_1x2 第 4 路分量, 权重 0.1. "
             "子模块: pipeline.template_deviation_detector / template_deviation_api.",
    ),
}


# ═══════════════════════════════════════════════════════════════
#  已下线 —— 保留记录防止复活为"新 SSoT"
# ═══════════════════════════════════════════════════════════════
DEPRECATED: Dict[str, str] = {
    "pipeline.engine (WCEngine/LeagueEngine)":
        "前端零调用; 能力被 M1/M2 覆盖. 仅 /predict 遗留端点保活, 不再演进.",
    "pipeline.model_dispatcher":
        "死链 —— 全项目仅 tmp/ 临时脚本引用. 路由能力由 ranked_predictor 承担.",
    "pipeline.pattern_matcher":
        "死链 —— 仅被 model_dispatcher 调用.",
    "pipeline.model_registry (outcome_full18/25feat/top10/reversal_top12)":
        "死链 —— 仅被 model_dispatcher 与训练脚本引用.",
    "pipeline.gq_model (gq_1x2/gq_ou/gq_total)":
        "零线上引用; 结构信号能力由 M5 承担.",
    "pipeline.gq_dc_model + pricing_template_engine":
        "半死链; 模板能力由 M7 承担.",
    "saved_models/draw_expert_*":
        "WC 平局专家 5 个版本; 平局能力由 M1+M2 承担.",
    "saved_models/multi_* (2026-06-18 批次 21 个)":
        "早期多任务批次, 零引用.",
    "saved_models/football_*_production / chain3_*":
        "早期整体模型, 零线上引用, 合计 >280MB. (independent_model 已剔除本项: "
        "2026-08-17 重训并接入 ranked_predictor._independent_1x2, 为最强单模型, 非废弃, 不在 7 模型上限内.)",
}


# ═══════════════════════════════════════════════════════════════
#  LEGACY_PINNED —— 已下线但**物理保活**的权重
#  ------------------------------------------------------------
#  这些权重不属于 M1-M7 任何一个模型, 但被 bridge_service 启动路径硬加载:
#      bridge_service:84  ENGINE = create_engine("wc")
#      pipeline/engine.py create_engine() -> `if not engine.loaded: raise RuntimeError`
#      pipeline/feature_consistency.py    -> verify_feature_cols() 读 draw_expert 取 feature_cols
#  直接归档会导致服务启动失败, 因此**暂钉住**.
#  解钉条件: 把 WC/League 引擎改为惰性加载 + 允许失败降级后, 移入 DEPRECATED 并归档.
# ═══════════════════════════════════════════════════════════════
LEGACY_PINNED: Dict[str, str] = {
    "saved_models/wc_main_v1.joblib":
        "bridge 启动时 create_engine('wc') 硬依赖",
    "saved_models/draw_expert_v3_focal.joblib":
        "wc_engine 平局门 + feature_consistency 的 feature_cols 来源",
}


def validate() -> None:
    """启动期自检: 模型数不得超过 MAX_MODELS, 且 ID 连续."""
    if len(CATALOG) > MAX_MODELS:
        raise ValueError(
            f"模型数 {len(CATALOG)} 超过上限 {MAX_MODELS}. "
            f"新增模型前必须先下线一个 —— 见 DEPRECATED."
        )
    expect = [f"M{i}" for i in range(1, len(CATALOG) + 1)]
    if sorted(CATALOG.keys()) != sorted(expect):
        raise ValueError(f"模型 ID 不连续: {sorted(CATALOG.keys())} != {expect}")


def active_models() -> List[ModelSpec]:
    return [m for m in CATALOG.values() if m.status == "active"]


def all_weights(include_pinned: bool = True) -> List[str]:
    """当前必须保留在磁盘上的权重 —— 不在此列表的 .joblib 均可归档.

    Args:
        include_pinned: True 则含 LEGACY_PINNED (启动硬依赖, 默认保留).
    """
    out: List[str] = []
    for m in CATALOG.values():
        out.extend(m.weights)
    if include_pinned:
        out.extend(LEGACY_PINNED.keys())
    return out


def summary() -> str:
    lines = [f"哨响AI 模型目录 (上限 {MAX_MODELS}, 当前 {len(CATALOG)})", "=" * 74]
    for mid in sorted(CATALOG):
        m = CATALOG[mid]
        flag = {"active": "●", "degraded": "◐", "shadow": "○"}.get(m.status, "?")
        lines.append(f"{flag} {m.mid}  {m.name:<16} [{','.join(m.tasks)}]")
        lines.append(f"     {m.role}")
        lines.append(f"     module : {m.module}")
        if m.weights:
            lines.append(f"     weights: {', '.join(m.weights)}")
        if m.note:
            for i, seg in enumerate(m.note.split(". ")):
                if seg.strip():
                    lines.append(f"     {'note   :' if i == 0 else '        '} {seg.strip()}")
        lines.append("")
    lines.append(f"已下线 ({len(DEPRECATED)} 组):")
    for k, v in DEPRECATED.items():
        lines.append(f"  ✗ {k}\n      {v}")
    return "\n".join(lines)


validate()

if __name__ == "__main__":
    print(summary())
