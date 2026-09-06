"""
pipeline.ranked_predictor — 概率排名编排器 (SSoT, 2026-07-30 用户纠偏后)

产品方向纠偏: 用户 2026-07-30 取消"OU 设为主模型", 改为按概率排名编排(OU/CS 不特权);
2026-08-03 进一步授权 **1X2 由 WI(威廉+Inter 真实特征训练)模型主导** (对"不预置任何市场为主"的例外).
本编排器:
  - 1X2: WI 教师概率主导(0.85) + 庄家隐含去水锚定地板(0.15, 盘口锚定铁律).
  - OU / CS: 各自锚定操盘手赔率去水, 不享特权.
  - 跨市场按概率降序**统一排名** → 最高概率者自然成为主结论.
  - 操盘手解读 (reverse_odds_engine) 附于结论, 作意图/陷阱校验, 不覆盖概率排名.

铁律: 全程复用 SSoT 组件 (ou_eval / score_model / reverse_odds_engine), 禁平行重造.

对外契约: `predict()` 返回内部丰富结构; `to_api_contract()` 将其映射为与 bridge
`/predict/single` 兼容的 API 响应 (prediction/probabilities/market_baseline/score/
score_prediction/analysis...), 供后续接入前端时直接消费, 当前不接前端.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# 保证既可作为 `python pipeline/ranked_predictor.py` 直接跑, 也可被 bridge 以 `pipeline.ranked_predictor` 导入
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 放在 _ROOT 注入 sys.path 之后, 避免直接 `python pipeline/ranked_predictor.py` 运行时找不到 pipeline 包
from pipeline.model_ensemble import DEFAULT_ENSEMBLE_1X2, DEFAULT_ENSEMBLE_TOTAL

# 单一真相源: 导入即触发 model_catalog.validate(), 在编排器加载期强制 7 模型上限
# (2026-08-22 重建: 此前 validate() 从未被任何运行时模块调用, 上限是纸面纪律)。
# 若模型数超 MAX_MODELS 或 ID 不连续, 此处直接抛错阻断启动, 防止模型面再次膨胀。
import pipeline.model_catalog  # noqa: F401

PARAMS_PATH = os.path.join(os.path.dirname(__file__), "ranked_params.json")

DEFAULT_PARAMS = {
    "base_gs": 1.2,        # score_model 通用联赛 goal_scale (SSoT 校准值)
    "cs_w_market": 1.0,    # CS 排名中操盘手CS赔率权重 (锚定铁律: 高=跟盘; 训练证实=1.0最优)
    "max_goal": 8,
    # ── 1X2 集成权重 (单一真相源 = model_ensemble.DEFAULT_ENSEMBLE_1X2) ──
    # WI 教师主导 0.85; devig_raw 0.15 锚定地板(盘口锚定铁律). wi_student 已移出(与教师100%共线).
    "ensemble_1x2": DEFAULT_ENSEMBLE_1X2,
    # ── 总进球集成权重 (单一真相源) ──
    "ensemble_total": DEFAULT_ENSEMBLE_TOTAL,
    # ── 先后: 跨庄软线价差(真 edge) 检测到时覆盖集成 (最高优先级, 见 model_ensemble.MODEL_ORDER) ──
    "priority_override": "cross_book_edge",
    # legacy 回退字段(兼容旧调用); 集成逻辑已改用 ensemble_*, 此值不参与融合
    "wi_1x2_weight": 0.6,
    # ── 特征库结构信号 (fl_model_*.joblib) 融合权重 ──
    # 该信号由 GQ 单庄结构库训练的树模型产出, 纯赔率结构推导.
    # 1X2/OU 弱于现有 SSoT、AH 有 +11.9pp 正信号. 默认 0.0 = 仅透明展示、不融合(零回归).
    # 2026-08-08 用户授权受控开启: 设 0.1 把 fl_model_1x2 以 10% 权重混入 1X2 最终概率
    # (零回归基准已存档 fl_model_20260805_preretrain/; 若回测显示拖累可瞬间回 0.0).
    # 设 >0 即把 fl_structure 混入最终概率.
    "fl_structure_weight": 0.1,
    # ── 独立融合模型 (independent_model.joblib) 融合权重 ──
    # 2026-08-17 重训并接入(M1): 学'球队实力 vs 市场定价'残差, OOF 宏AUC 0.6807
    # 优于市场基线 0.6590; 分歧处净 edge +0.079. 默认 0.15 受控开启,
    # 设 0.0 即零回归(不融合); 与 fl_structure 同款后融合, 不扰动 wi/devig 锚定比.
    "independent_weight": 0.15,
    # ── 半场条件大小球 (inplay_ou) 融合权重 ──
    # 编码用户经验性结构特征(涛哥 2026-08-03): 半场比分+剩余时间窗口 → 条件大球率.
    # 数据基础 = data/ht_conditional_ou.json (football_data.db 1829 场 HT+全场).
    # HT 1:1 → 大3.5 = 45.0% (无条件 30.1%, 结构性 +15pp). 此为 IN-PLAY 信号(需已知半场比分).
    # 默认 0.0 = 仅透明展示、不融合(零回归); 需受控开启(>0 即把 inplay_conditional 混入 OU 概率).
    "inplay_ou_weight": 0.0,
    # ── 扫盘模型(分析中心)校准权重 ──
    # AnalysisCenter 相似盘口历史经验频率(home/draw/away 分布)作为 blend_1x2 第4路分量.
    # 默认 0.1 = 真校准本期开(用户 2026-08-04 授权); 设 0.0 即零回归(不融合).
    # 仅在 analysis_center_weight>0 时触发邻居向量库查询(带120s缓存+失败降级), 默认路径零延迟.
    "analysis_center_weight": 0.1,
}

_LABEL_TO_CODE = {"主胜": "H", "平局": "D", "客胜": "A"}


def load_params() -> Dict[str, float]:
    p = dict(DEFAULT_PARAMS)
    if os.path.exists(PARAMS_PATH):
        try:
            p.update(json.load(open(PARAMS_PATH, encoding="utf-8")))
        except Exception:
            pass
    return p


def save_params(p: Dict[str, float]) -> None:
    json.dump(p, open(PARAMS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _lazy():
    from pipeline.evaluation.ou_eval import grade_direction, ou_devig, calibrate_ou_under
    from pipeline.score_model import predict_score
    from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
    from pipeline.ah_eval import evaluate_ah
    return grade_direction, ou_devig, calibrate_ou_under, predict_score, ReverseOddsEngine, OddsInput, evaluate_ah


def _devig(*odds) -> List[float]:
    """去水: 1/odds 归一化 → 概率分布. odds<=1.01 视为无效跳过.

    注意: 本函数是**变参且会丢元素**的, 返回长度 <= 入参长度. 仅适用于
    波胆那种"列表进列表出、位置无语义"的场景. 需要位置对齐的定长市场
    (如 1X2 的 p_h/p_d/p_a) 必须用 _devig3, 否则解包会 ValueError.
    """
    inv = [1.0 / float(o) for o in odds if o and float(o) > 1.01]
    z = sum(inv) or 1.0
    return [v / z for v in inv]


def _devig3(h, d, a) -> List[float]:
    """1X2 专用去水: 恒返回 3 个位置对齐的概率.

    不能复用变参 _devig —— 它会丢弃 <=1.01 的赔率, 使返回长度 <3,
    导致 `p_h, p_d, p_a = _devig(h, d, a)` 抛 ValueError.
    实测 GQ 全库 569/3772 (15.08%) 场次含 <=1.01 的 1X2 赔率而整场 predict() 崩溃.

    对非法/哨兵赔率(0.99, 1.0 等)采用**钳位**而非丢弃: 1.01 是合法极端报价
    (隐含 99%), 而 0.99 这类脏数据在语境上仍表示"压倒性热门", 若丢弃或置 0
    会把该侧概率算成 0 → 预测方向完全反转, 比崩溃更危险.
    对所有正常赔率(三边均 >1.01), 本函数与原 _devig 输出逐位完全相同.
    """
    inv = [(1.0 / max(float(o), 1.01)) if o else 0.0 for o in (h, d, a)]
    z = sum(inv) or 1.0
    return [v / z for v in inv]


def _independent_1x2(home: str, away: str, h: float, d: float, a: float):
    """独立融合模型(independent_model.joblib) 1X2 概率. 失败优雅降级 None.

    学的是'球队实力 vs 市场定价'残差, 在分歧处带真实 edge (OOF 净 edge +0.079).
    2026-08-17 重训并接入(M1): 该 joblib 此前在模型收敛清理中被误判零引用一并清除,
    实为最强单模型. 推理端点见 pipeline.independent_predictor (零回归守卫: 查不到特征即 None).
    """
    try:
        from pipeline.independent_predictor import predict_1x2 as _indep_predict
        return _indep_predict(home, away, float(h), float(d), float(a))
    except Exception:
        return None


def _wi_1x2(h: float, d: float, a: float):
    """主导模型(WI 教师)校准概率. 惰性导入, 失败优雅降级为 None (不影响其他模型).

    训练语料: 威廉(2012-2018)+Inter(2016-2025) 共 ~59.9万场 1X2 盘口+赛果.
    校准极佳(预测P(H)≈实际频率), 单庄无 pick-edge(与庄家热门基线 -0.18pp),
    但提供比原始隐含概率更可靠的概率主干. 与盘口锚定铁律不冲突(同源派生, 仅去偏).
    """
    try:
        from pipeline.william_inter_model import predict_1x2
        # 入口仅有收盘赔率(无开盘) → open=close (drift=0, 模型可接受)
        r = predict_1x2(float(h), float(d), float(a), float(h), float(d), float(a))
        return r["proba"]  # [p_h, p_d, p_a]
    except Exception:
        return None


# wi_student 已从集成热路径移除: 与 WI 教师 100% 共线, 无独立信号.
# 轻量校准需求改用 william_inter_model.calibrate_devig 独立调用.


def _jepa_1x2(home: str, away: str, h: float, d: float, a: float):
    """JEPА/OIP 比分模型的 1X2 概率 (来自 deoverround, =devig_raw; 主要用于波胆市场)."""
    try:
        from pipeline.score_model import predict_score
        r = predict_score(home, away, float(h), float(d), float(a))
        return [float(r["p_h"]), float(r["p_d"]), float(r["p_a"])]
    except Exception:
        return None


def _wi_total(h: float, d: float, a: float):
    """WI 总进球期望. 失败返回 None."""
    try:
        from pipeline.william_inter_model import predict_total
        return float(predict_total(float(h), float(d), float(a), float(h), float(d), float(a)))
    except Exception:
        return None


def _jepa_total(home: str, away: str, h: float, d: float, a: float):
    """JEPА/OIP 期望总进球 (lh+la). 失败返回 None."""
    try:
        from pipeline.score_model import predict_score
        r = predict_score(home, away, float(h), float(d), float(a))
        return float(r["lh"]) + float(r["la"])
    except Exception:
        return None


def _parse_cs(op_cs) -> List[Tuple[str, float]]:
    """op_cs: JSON 串 [['1-1',8.3],...] 或已解析列表 → [(score, odds)] (odds>1.01)."""
    if op_cs is None:
        return []
    if isinstance(op_cs, str):
        try:
            op_cs = json.loads(op_cs)
        except Exception:
            return []
    out = []
    for row in op_cs:
        try:
            s, o = row[0], float(row[1])
            if o > 1.01:
                out.append((str(s), o))
        except Exception:
            continue
    return out


def _compute_confidence(m1x2, mou, mcs, direction, grade, intent,
                        p_h, p_d, p_a, ah_direction=None) -> Dict[str, Any]:
    """把握度分级 — 把模型内部'有把握'的信号汇成人工可一眼识别的结论.

    综合六维度:
      1. direction_margin_pp : 1X2 榜首概率 - 次席概率 (方向清晰度; 越大越有把握)
      2. cross_market_consistent: 1X2 榜首方向 与 波胆榜首方向(主/平/客)是否自洽
      3. operator_intent     : 诚实防范(honest_def*)→印证 / 诱盘(fake_def*)→降权 / neutral→基线
      4. is_trap             : OU 盘口判为陷阱盘(trap)→反向, 整体降权
      5. edge_severity       : 跨庄离散严重度(HIGH≥15/MED≥10/LOW≥5pp), 真 edge 硬证据(留接口)
      6. ah_consistent       : 亚盘让球方向(主/客被看好) 与 1X2 榜首方向 是否自洽 (新增, 让球链路)

    输出: tier(高/中/低) + score(0-100) + factors(各维度明细) + reasons(人读理由).
    分级用规则判定(可控), score 用于前端强度条.
    """
    # 1. 方向清晰度
    top_lbl, top_p = m1x2[0]
    snd_p = m1x2[1][1] if len(m1x2) > 1 else 0.0
    margin_pp = round((top_p - snd_p) * 100, 1)
    x2_dir = _LABEL_TO_CODE.get(top_lbl, "H")

    # 2. 跨市场一致性 (1X2 方向 vs 波胆榜首方向)
    cross_ok = False
    if mcs:
        try:
            ci, cj = mcs[0][0].split("-", 1)
            ci, cj = int(ci), int(cj)
            cs_dir = "H" if ci > cj else ("A" if ci < cj else "D")
            cross_ok = (cs_dir == x2_dir)
        except Exception:
            cross_ok = False

    # 3 & 4. 庄家意图 / 陷阱盘
    honest = intent in ("honest_defH", "honest_defA")
    fake = intent in ("fake_defH", "fake_defA")
    neutral = intent in ("neutral", "", None)
    # grade 实际取值带后缀 (trap_low / trap_high / trap_high_side; 诚实盘经市场感知
    # 还会加 _mkt 后缀, 见 pipeline/evaluation/ou_eval.py:84), 字面量 "trap" 从不出现.
    # 原先写 `grade == "trap"` 导致 is_trap 恒 False → 陷阱盘 100% 漏报且被误标"诚实盘".
    # 子串判定与 pipeline/predictors/ou_linkage.py 既有惯例一致 (该文件 4 处均用 'trap' in grade).
    is_trap = ("trap" in (grade or ""))

    # 6. 亚盘让球一致性 (让球方向 与 1X2 榜首方向自洽)
    ah_consistent = False
    if ah_direction and x2_dir != "D":
        ah_consistent = (ah_direction == ("主队" if x2_dir == "H" else "客队"))

    # 5. 跨庄 edge (实时场多数无此数据, 留接口, 默认空)
    edge_severity = ""

    # ── 综合评分 (0-100) ──
    score = 20  # 基线
    if margin_pp >= 15: score += 45
    elif margin_pp >= 10: score += 32
    elif margin_pp >= 6: score += 18
    else: score += 4
    score += 22 if cross_ok else 4
    if honest: score += 14
    elif neutral: score += 5
    if is_trap: score -= 18
    if ah_consistent: score += 8
    score = max(0, min(100, score))

    # ── 分级 (规则判定, 可控) ──
    # 核心: 方向清晰(margin≥15) + 庄家不反对(非陷阱/非诱盘) = 高把握.
    # 跨市场一致性(cross_ok)是强加分项(影响 score), 但不做'高'档硬门槛(避免把清晰场压成中).
    if is_trap:
        tier = "低" if margin_pp < 12 else "中"
    elif margin_pp >= 15 and (honest or neutral):
        tier = "高"
    elif (margin_pp >= 8 and (honest or neutral)) or cross_ok:
        tier = "中"
    else:
        tier = "低"

    # ── 人读理由 ──
    reasons = []
    if margin_pp >= 15:
        reasons.append(f"方向清晰(榜首领先{margin_pp}pp)")
    elif margin_pp >= 8:
        reasons.append(f"方向较清晰(领先{margin_pp}pp)")
    else:
        reasons.append(f"方向模糊(领先仅{margin_pp}pp, 三分天下)")
    reasons.append("1X2与波胆榜首方向自洽" if cross_ok else "1X2与波胆方向不完全一致(长尾)")
    if honest:
        reasons.append("操盘手诚实防范, 与概率排名相互印证")
    elif fake:
        reasons.append("检测到诱盘信号, 已谨慎处理")
    if is_trap:
        reasons.append("大小球判为陷阱盘, 已降权")

    return {
        "tier": tier,
        "score": score,
        "factors": {
            "direction_margin_pp": margin_pp,
            "cross_market_consistent": cross_ok,
            "operator_intent": intent,
            "is_trap": is_trap,
            "edge_severity": edge_severity,
            "ah_consistent": ah_consistent,
        },
        "reasons": reasons,
    }


def _build_analysis(home, away, m1x2, p_h, p_d, p_a,
                    line, direction, grade, p_over, p_under, ou_read, mou,
                    mcs, cs_read, intent, operator_verdict, combined_top) -> Dict[str, str]:
    """把三市场概率 + 操盘手解读, 写成完整中文分析文案 (供前端 MatchAnalysisModal 渲染)."""
    top_label, top_p = combined_top[0]
    top1x2_lbl, top1x2_p = m1x2[0]
    pred_code = _LABEL_TO_CODE.get(top1x2_lbl, "H")

    verdict = (
        f"综合最高概率结论 = {top_label} (P={top_p:.1%}). "
        f"1X2 由威廉+Inter 真实特征训练的 WI 模型主导; OU/CS 不享特权, 按概率排名."
    )

    x2 = f"独赢: 主胜 {p_h:.1%} / 平 {p_d:.1%} / 客胜 {p_a:.1%} · 最可能 {top1x2_lbl} ({top1x2_p:.1%})"

    if line and mou:
        ou_note = "陷阱盘(反向), 不盲跟低赔侧" if "trap" in (grade or "") else "诚实盘, 跟市场低赔侧"
        ou = f"大小球 {line}: {direction} · 大 {p_over:.1%} / 小 {p_under:.1%} · {ou_note}"
    else:
        ou = "无有效大小球盘口"

    cs_top_s, cs_top_p = mcs[0] if mcs else ("0-0", 0.0)
    cs = f"波胆首选 {cs_top_s} ({cs_top_p:.1%}) · {cs_read}"

    if intent in ("fake_defH", "fake_defA"):
        op_note = "检测到诱盘(假防)信号, 仅作校验不覆盖"
    elif intent in ("honest_defH", "honest_defA"):
        op_note = "诚实防范, 与概率排名相互印证"
    else:
        op_note = "单快照无开盘→收盘漂移, 仅基线校验"
    operator = f"操盘手意图: {intent} · {op_note}"

    # ── 动态风险提示 (从 model_ensemble 读真实基准, 不再硬编码) ──
    try:
        from pipeline.model_ensemble import BENCHMARK_1X2_GAIN, BENCHMARK_OU_GAIN
        x2_gain = BENCHMARK_1X2_GAIN
        ou_gain = BENCHMARK_OU_GAIN
    except Exception:
        x2_gain, ou_gain = 8.35, 0.51
    x2_note = f"+{x2_gain:.1f}pp(显著)" if x2_gain > 5 else f"+{x2_gain:.1f}pp"
    ou_note = f"+{ou_gain:.1f}pp(微弱)" if ou_gain < 2 else f"+{ou_gain:.1f}pp"
    risk = (
        f"风险提示: 1X2 模型相对'永远买主胜'基线 {x2_note}; "
        f"OU/波胆增益{ou_note}, 长赔率数学期望为负, 仅作分析参考."
    )

    ranking = "; ".join(f"{i+1}. {lbl} {p:.1%}" for i, (lbl, p) in enumerate(combined_top))

    return {
        "verdict": verdict,
        "1x2": x2,
        "ou": ou,
        "cs": cs,
        "operator": operator,
        "risk": risk,
        "ranking": ranking,
    }


def predict(home: str, away: str,
            h: float, d: float, a: float,
            ou_line=None, ou_over=None, ou_under=None,
            op_cs=None, ah_line=None, ah_home=None, ah_away=None,
            league: Optional[str] = None,
            inplay: Optional[Dict[str, Any]] = None,
            params: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """概率排名预测. 返回四市场概率分布(1X2/OU/CS/AH) + 跨市场统一排名 + 操盘手解读 + 完整分析文案."""
    grade_direction, ou_devig, calibrate_ou_under, predict_score, ReverseOddsEngine, OddsInput, evaluate_ah = _lazy()
    # params 合并而非替换: 传入部分参数时保留 DEFAULT_PARAMS 其余默认值(避免 KeyError)
    if params is None:
        params = load_params()
    else:
        _base = load_params()
        _base.update(params)
        params = _base

    # ── 特征库结构信号 (fl_model_*.joblib, 纯赔率结构推导) ──
    # 作为 ranked_predictor 一路独立特征输入. 默认 fl_structure_weight=0.0 → 仅透明展示、不融合.
    try:
        from pipeline.fl_predictor import predict_from_odds as _fl_predict
        fl_signal = _fl_predict(
            h=h, d=d, a=a,
            ou_line=ou_line, ou_over=ou_over, ou_under=ou_under,
            op_cs=op_cs, ah_line=ah_line, ah_home=ah_home, ah_away=ah_away,
            league=league, kickoff=None,
        )
    except Exception:
        fl_signal = {"1x2": None, "ou": None, "ah": None}

    # ── 市场1: 1X2 概率 (锚定操盘手独赢赔率) ──
    devig_raw = _devig3(h, d, a)  # 庄家隐含去水 = 其他模型(JEPА/unified)的 1X2 基线
    p_h, p_d, p_a = devig_raw
    m1x2 = sorted([("主胜", p_h), ("平局", p_d), ("客胜", p_a)], key=lambda x: -x[1])

    # ── WI 主导集成 (集成权重 + 先后) ──
    # 组件: wi_teacher(WI教师, 主导) / devig_raw(庄家基线, 锚定地板) / jepa_dc(交叉校验)
    # 见 pipeline.model_ensemble (MODEL_ORDER; cross_book_edge 最高优先级覆盖)
    # 注: wi_student 已验证与教师 100% 共线, 移出热路径; 轻量校准见 william_inter_model.calibrate_devig
    from pipeline.model_ensemble import blend_1x2, blend_total

    e1 = params.get("ensemble_1x2") or DEFAULT_ENSEMBLE_1X2
    et = params.get("ensemble_total") or DEFAULT_ENSEMBLE_TOTAL

    wi_teacher = _wi_1x2(h, d, a)
    jepa_dc = _jepa_1x2(home, away, h, d, a)

    # ── 扫盘模型(分析中心)校准信号 ──
    # 单场赔率结构指纹(score/verdict/warns) 本地零延迟, 透明展示;
    # 相似盘口经验频率(neighbor_freq) 作第4路分量, 仅 ac_w>0 时查向量库(带缓存+降级).
    ac_w = float(params.get("analysis_center_weight", 0.0) or 0.0)
    try:
        from pipeline.analysis_center import _analyze_odds
        _ac = _analyze_odds(h, d, a, ou_over, ou_under, with_neighbors=(ac_w > 0))
    except Exception:
        _ac = {"score": None, "verdict": None, "signals": [], "warns": [], "poisson": None, "neighbor_freq": None}
    ac_freq = _ac.get("neighbor_freq") if ac_w > 0 else None
    # 权重注入: 仅当分量真实可用时才把 analysis_center 纳入 blend_1x2 的归一化权重集
    # (ac_freq=None → 分量被 blend_1x2 自动忽略 + e1 不注入 = 严格零回归)
    if ac_w > 0 and ac_freq is not None:
        e1 = dict(e1); e1["analysis_center"] = ac_w

    components = {
        "wi_teacher": wi_teacher,
        "devig_raw": devig_raw,
        "jepa_dc": jepa_dc,
        # 特征库结构信号(纯赔率结构推导, 透明展示; 默认不融合, 见 fl_structure_weight)
        "fl_structure": fl_signal["1x2"],
        # 扫盘模型(分析中心)相似盘口历史经验频率(见 analysis_center_weight; None 时自动忽略)
        "analysis_center": ac_freq,
    }

    # 先后·覆盖级: 跨庄软线价差(真 edge) 检测到即覆盖集成结果 (优雅降级: 无跨庄数据则 None)
    override = None
    if params.get("priority_override") == "cross_book_edge":
        try:
            from pipeline.cross_book_edge import detect_edge
            ov = detect_edge(home, away, h, d, a)
            if ov and isinstance(ov, (list, tuple)) and len(ov) == 3:
                override = [float(x) for x in ov]
        except Exception:
            override = None

    blended, overridden = blend_1x2(components, e1, override=override)
    p_h, p_d, p_a = blended
    # ── 校准偏置叠加 (tick 尾数信号, 31.5万行赛果验证 p<0.001) ──
    # 仅当原始赔率(h/d/a)在1.0-1.49大热门区才触发, 覆盖~8.6%场次.
    # 此前仅老路径 /predict/single 有, 路径 /api/predict/ranked 缺失 (2026-08-04 修复).
    _calib_applied = []
    try:
        from pipeline.calibration_overlay import apply_1x2_overlay
        p_h, p_d, p_a, _calib_applied = apply_1x2_overlay(p_h, p_d, p_a, h, d, a)
    except Exception:
        pass
    # ── 受控融合: fl_structure 结构信号 (默认权重0, 不生效, 零回归) ──
    fl_w = float(params.get("fl_structure_weight", 0.0) or 0.0)
    if fl_w > 0 and fl_signal["1x2"] and not overridden:
        p_h = (1 - fl_w) * p_h + fl_w * fl_signal["1x2"][0]
        p_d = (1 - fl_w) * p_d + fl_w * fl_signal["1x2"][1]
        p_a = (1 - fl_w) * p_a + fl_w * fl_signal["1x2"][2]
    # ── 受控融合: independent 独立融合模型 (默认权重0.15, 可回0零回归) ──
    # 学'球队实力 vs 市场定价'残差, OOF 净 edge +0.079. 仅当特征查得出来才贡献权重,
    # 否则 indep_p=None → 零回归(与 fl_structure / analysis_center 同款守卫).
    indep_w = float(params.get("independent_weight", 0.0) or 0.0)
    indep_p = None
    if indep_w > 0 and not overridden:
        indep_p = _independent_1x2(home, away, h, d, a)
        if indep_p:
            p_h = (1 - indep_w) * p_h + indep_w * indep_p[0]
            p_d = (1 - indep_w) * p_d + indep_w * indep_p[1]
            p_a = (1 - indep_w) * p_a + indep_w * indep_p[2]
    m1x2 = sorted([("主胜", p_h), ("平局", p_d), ("客胜", p_a)], key=lambda x: -x[1])

    # ── 总进球集成 (WI 总进球 + JEPА 期望) ──
    wi_tot = _wi_total(h, d, a)
    jepa_tot = _jepa_total(home, away, h, d, a)
    expected_total_raw = blend_total(
        {"wi_total": wi_tot, "jepa_score": jepa_tot}, et
    )

    # ── 联赛/赛事进球水平先验 (2026-08-12: 独立特征 + 校准 + 零回归) ──
    # 把"赛事/联赛场均总球"作为合法先验收缩混合进中心 λ. 高流动性赛事(赔率已含)
    # 先验权重≈0.05 (几乎只看赔率); obscure/未知赛事权重升到 0.25 (把噪声总球拉回
    # 联赛/全局均值). 仅修正中心预期总球, 不改 OU方向/1X2/verdict (决策链路零回归).
    league_scoring = None
    expected_total = expected_total_raw
    try:
        from pipeline.league_scoring_prior import blend_total_with_league
        # 仅当 league 提供时应用先验: league 缺失 → 完全跳过 (零回归)
        if league:
            _ls = blend_total_with_league(expected_total_raw, league)
            league_scoring = {
                "prior_total": _ls["prior_mean"],
                "prior_n": _ls["prior_n"],
                "matched_league": _ls["matched_league"],
                "liquidity": _ls["liquidity"],
                "weight": _ls["weight"],
                "method": _ls["method"],
                "raw_total": _ls["odds_total"],
            }
            expected_total = _ls["adjusted"]
    except Exception:
        pass

    # ── 市场2: OU 概率 + 盘口读 ──
    line = float(ou_line) if ou_line else 0.0
    direction, grade = "NEUTRAL", "none"
    p_over = p_under = 0.0
    ou_read = "无大小球盘口"
    mou = []
    # 注意: inplay_over 必须与上面几个 OU 变量同级初始化 (不能只在 if 块内赋值),
    # 否则无 OU 盘口的场次走不进下面的 if, 而 markets.ou.inplay_conditional 仍会引用它
    # → UnboundLocalError, 整场 predict() 崩溃 (2026-08-05 修复).
    inplay_over = None
    if line > 0 and ou_over and ou_under and float(ou_over) > 1.01 and float(ou_under) > 1.01:
        direction, grade = grade_direction(line, float(ou_over), float(ou_under))
        p_over, p_under = ou_devig(float(ou_over), float(ou_under))
        # ── 0-2 球段下盘校准 (2026-08-03): 庄家系统性低估下盘时温和上修 ──
        cal = calibrate_ou_under(p_under, line, league)
        p_over, p_under = cal["p_over"], cal["p_under"]
        # 受控融合: fl_structure OU 信号 (默认权重0, 不生效)
        if fl_w > 0 and fl_signal["ou"]:
            p_over = (1 - fl_w) * p_over + fl_w * fl_signal["ou"][0]
            p_under = (1 - fl_w) * p_under + fl_w * fl_signal["ou"][1]

        # ── 半场条件大小球 (inplay_ou): 编码用户经验结构特征 ──
        # IN-PLAY 信号: 仅当调用方已知半场比分(inplay 含 ht_home/ht_away) 时生效.
        # 默认 inplay_ou_weight=0 → 仅透明展示(inplay_conditional), 不融合(零回归).
        if inplay and "ht_home" in inplay and "ht_away" in inplay:
            try:
                from pipeline.inplay_ou import predict as _inplay_predict
                _ip = _inplay_predict(
                    int(inplay["ht_home"]), int(inplay["ht_away"]), line,
                    minutes_remaining=float(inplay.get("minutes_remaining", 45.0)),
                    price=(float(inplay["price"]) if inplay.get("price") else None),
                )
                inplay_over = _ip
                ip_w = float(params.get("inplay_ou_weight", 0.0) or 0.0)
                if ip_w > 0 and _ip.get("over_prob") is not None:
                    p_over = (1 - ip_w) * p_over + ip_w * _ip["over_prob"]
                    p_under = 1.0 - p_over
            except Exception:
                inplay_over = None

        mou = sorted([("大球", p_over), ("小球", p_under)], key=lambda x: -x[1])
        trap = "陷阱盘(反向)" if "trap" in (grade or "") else "诚实盘(跟市场低赔侧)"
        cal_tag = (f" | 下盘校准+{cal['delta_pp']:.1f}pp(真值{cal['truth']:.0%},{cal['league_used']})"
                   if cal["calibrated"] else "")
        ou_read = f"{line} 球线 → {direction} (grade={grade}, {trap}); P大={p_over:.2f}/P小={p_under:.2f}{cal_tag}"

    # ── 市场3: CS 波胆概率 (操盘手CS锚定 + OIP) ──
    sc = predict_score(home, away, h, d, a,
                       max_goal=int(params["max_goal"]), goal_scale=float(params["base_gs"]))

    # ── 市场4: 亚盘让球 (AH) — 数据来自 GQ (用户已采集, 仅界面设了显示欧赔) ──
    # 盘口锚定铁律: 去水即公平概率, 方向跟着盘口低赔侧(被看好方)走. 无 AH 数据则降级 None.
    ah = None
    # 注意: 平手盘 line=0.0 是 falsy, 必须用 `is not None` 判定, 否则 GQ 快照里
    # 占比 73% 的 AH_0.00 会被静默丢弃 (2026-08-01 回测 n=2 的根因).
    if ah_line is not None and ah_home and ah_away:
        try:
            ah = evaluate_ah(ah_line, ah_home, ah_away)
        except Exception:
            ah = None
    # 完整 OIP 分布 (覆盖所有比分, 不只 top5) → 用于波胆 EV 计算与 blended 融合, 比 top5 截断更准
    _M = sc.get("matrix")
    if _M is not None:
        oip_prob = {f"{i}-{j}": float(_M[i, j]) for i in range(_M.shape[0]) for j in range(_M.shape[1])}
    else:
        oip_prob = {f"{i}-{j}": p for i, j, p in sc["top_scores"]}
    cs_list = _parse_cs(op_cs)
    wm = float(params["cs_w_market"])
    if cs_list:
        mkt = {s: p for s, p in zip([s for s, _ in cs_list], _devig(*[o for _, o in cs_list]))}
        blended = {}
        for s, mp in mkt.items():
            op = oip_prob.get(s, 0.0)
            blended[s] = wm * mp + (1.0 - wm) * op
        z = sum(blended.values()) or 1.0
        blended = {k: v / z for k, v in blended.items()}
        mcs = sorted(blended.items(), key=lambda x: -x[1])[:5]
        cs_read = "锚定操盘手CS赔率(权重=%.2f) + OIP 融合" % wm
    else:
        mcs = [(f"{i}-{j}", p) for i, j, p in sc["top_scores"][:5]]
        cs_read = "无操盘手CS赔率 → 纯OIP Poisson 排名"

    # ── 波胆 EV (模型独立概率 vs 操盘手赔率) ──
    # EV = p_oip * odds - 1: 反映'模型比市场乐观'的 alpha (非跨庄价差).
    # 仅 op_cs 有赔率时算起; 无赔率(纯OIP)则无法算, 优雅降级 has_odds=False.
    cs_ev: Dict[str, Dict[str, float]] = {}
    if cs_list:
        for s, o in cs_list:
            po = oip_prob.get(s)
            if po is not None and o > 1.01:
                cs_ev[s] = {"prob": round(po, 4), "odds": round(o, 4), "ev": round(po * o - 1.0, 4)}

    # ── 跨市场统一概率排名 (OU 不特权) ──
    combined = [("1X2·" + lbl, p) for lbl, p in m1x2]
    combined += [("OU·" + lbl, p) for lbl, p in mou]
    combined += [("CS·" + s, p) for s, p in mcs]
    combined_top = sorted(combined, key=lambda x: -x[1])[:8]

    # ── 操盘手解读 (锚定铁律: 仅作校验, 不覆盖概率) ──
    operator_verdict, intent = "", "neutral"
    try:
        eng = ReverseOddsEngine()
        # 单次快照无开盘价 → open=close=当前赔率 (drift=0 → 意图 NEUTRAL).
        # 符合盘口锚定铁律: 无开盘→收盘漂移证据时不判陷阱, 仅作基线校验.
        oi = OddsInput(open_h=h, open_d=d, open_a=a, close_h=h, close_d=d, close_a=a)
        res = eng.analyze(oi)
        operator_verdict = res.verdict
        intent = res.intent.value
    except Exception as e:
        operator_verdict = f"(解读失败: {e})"

    # ── 把握度分级 (人工可识别的有把握信号) ──
    conf = _compute_confidence(m1x2, mou, mcs, direction, grade, intent, p_h, p_d, p_a,
                               ah_direction=(ah["direction"] if ah else None))

    # ── 完整分析文案 (供前端渲染) ──
    analysis = _build_analysis(home, away, m1x2, p_h, p_d, p_a,
                               line, direction, grade, p_over, p_under, ou_read, mou,
                               mcs, cs_read, intent, operator_verdict, combined_top)

    return {
        "home": home, "away": away,
        "confidence_tier": conf["tier"],
        "confidence_score": conf["score"],
        "confidence_factors": conf["factors"],
        "confidence_reasons": conf["reasons"],
        "markets": {
            "1x2": {"ranked": [(lbl, round(p, 4)) for lbl, p in m1x2], "p_h": round(p_h, 4), "p_d": round(p_d, 4), "p_a": round(p_a, 4),
                    # ── WI 主导集成明细 (蒸馏+集成权重+先后) ──
                    "components": {
                        "wi_teacher": [round(x, 4) for x in wi_teacher] if wi_teacher else None,
                        "devig_raw": [round(x, 4) for x in devig_raw],
                        "jepa_dc": [round(x, 4) for x in jepa_dc] if jepa_dc else None,
                        # 特征库结构信号(纯赔率结构推导; 默认不参与融合)
                        "fl_structure": [round(x, 4) for x in fl_signal["1x2"]] if fl_signal["1x2"] else None,
                        # 独立融合模型(球队实力 vs 市场残差; 默认 0.15 权重已融合, 此处透明展示)
                        "independent": [round(x, 4) for x in indep_p] if indep_p else None,
                        # 扫盘模型(分析中心)相似盘口经验频率(ac_w>0 时参与融合)
                        "analysis_center": [round(x, 4) for x in ac_freq] if ac_freq else None,
                    },
                    # 扫盘模型(分析中心)结构指纹: 透明展示, 不参与融合(融合走 components.analysis_center)
                    "analysis_center": {
                        "score": _ac.get("score"),
                        "verdict": _ac.get("verdict"),
                        "signals": _ac.get("signals"),
                        "warns": _ac.get("warns"),
                        "neighbor_freq": [round(x, 4) for x in ac_freq] if ac_freq else None,
                        "weight": ac_w,
                    },
                    "ensemble_weights": {k: round(v, 3) for k, v in e1.items()},
                    "override_applied": bool(overridden),
                    "expected_total": round(expected_total, 3) if expected_total is not None else None,
                    # 联赛/赛事进球水平先验 (2026-08-12 接入): 中心 λ 升级为联赛感知,
                    # expected_total = 收缩混合后; raw = 原 WI+JEPА 反演 (零回归审计用).
                    "expected_total_raw": round(expected_total_raw, 3) if expected_total_raw is not None else None,
                    "league_scoring": ({k: (round(v, 3) if isinstance(v, float) else v)
                                        for k, v in league_scoring.items()} if league_scoring else None),
                    "dominant": "wi_family"},
            "ou": {"line": line, "direction": direction, "grade": grade,
                   "p_over": round(p_over, 4), "p_under": round(p_under, 4),
                   "ranked": [(lbl, round(p, 4)) for lbl, p in mou], "read": ou_read,
                   # 特征库结构信号(纯赔率结构推导; 默认不参与融合)
                   "fl_structure": [round(x, 4) for x in fl_signal["ou"]] if fl_signal["ou"] else None,
                   # 半场条件大小球(用户经验结构特征; 默认不参与融合, 透明展示)
                   "inplay_conditional": inplay_over},
            "cs": {"ranked": [(s, round(p, 4)) for s, p in mcs], "read": cs_read,
                   "ev_map": cs_ev, "has_odds": bool(cs_ev)},
            # 亚盘让球: 结构信号作透明组件(不强融, 避免 symbol 错配; AH 路 fl 有 +11.9pp 正信号, 可后续受控开启)
            "ah": (dict(ah, **({"fl_structure": [round(x, 4) for x in fl_signal["ah"]]} if fl_signal["ah"] else {})) if ah else None),
        },
        "combined_top": [(lbl, round(p, 4)) for lbl, p in combined_top],
        "operator_intent": intent, "operator_verdict": operator_verdict,
        "analysis": analysis,
        "lambda": (sc.get("lh"), sc.get("la")),
    }


def to_api_contract(r: Dict[str, Any]) -> Dict[str, Any]:
    """把 predict() 的内部结构映射为与 bridge `/predict/single` 兼容的 API 响应契约.

    这样后续接入前端时, 前端按现有 MatchAnalysisModal 字段消费即可, 无需改字段名.
    `analysis` 字段由字符串升级为结构化 dict (verdict/1x2/ou/cs/operator/risk/ranking),
    向前兼容, 待接前端时由 modal 逐段渲染.
    """
    m1x2 = r["markets"]["1x2"]["ranked"]
    top_lbl, _ = m1x2[0]
    pred_code = _LABEL_TO_CODE.get(top_lbl, "H")
    pH, pD, pA = (r["markets"]["1x2"]["p_h"], r["markets"]["1x2"]["p_d"], r["markets"]["1x2"]["p_a"])

    cs_ranked = r["markets"]["cs"]["ranked"]
    cs_top_s, cs_top_p = cs_ranked[0] if cs_ranked else ("0-0", 0.0)
    try:
        sh, sa = cs_top_s.split("-", 1)
        score = {"home": int(sh), "away": int(sa)}
    except Exception:
        score = {"home": 0, "away": 0}
    top_scores = [{"score": s, "prob": round(p, 4), "outcome": pred_code} for s, p in cs_ranked[:3]]

    return {
        "prediction": pred_code,
        "result": pred_code,
        "probabilities": {"H": round(pH, 4), "D": round(pD, 4), "A": round(pA, 4),
                          "home": round(pH, 4), "draw": round(pD, 4), "away": round(pA, 4)},
        # 市场基线 = 去水隐含概率 (盘口锚定铁律: 操盘手赔率即地面真相)
        "market_baseline": {"H": round(pH, 4), "D": round(pD, 4), "A": round(pA, 4),
                            "prediction": pred_code},
        "score": score,
        "score_prediction": {"primary": cs_top_s, "top_scores": top_scores},
        "confidence": round(r["combined_top"][0][1], 4),
        "prediction_mode": "哨响AI-ranked-prob-v1 (三市场概率排名, OU不特权)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "analysis": r["analysis"],
        # ── 概率排名编排器专属字段 (前端 modal 可直接渲染) ──
        "markets": r["markets"],
        "combined_top": r["combined_top"],
        "operator_intent": r["operator_intent"],
        "operator_verdict": r["operator_verdict"],
        "lambda": r.get("lambda"),
        # ── 亚盘让球 (新增玩法, 数据来自 GQ AH 快照) ──
        "ah": r["markets"].get("ah"),
        # ── 把握度分级 (人工可识别的有把握信号) ──
        "confidence_tier": r.get("confidence_tier"),
        "confidence_score": r.get("confidence_score"),
        "confidence_factors": r.get("confidence_factors"),
        "confidence_reasons": r.get("confidence_reasons"),
    }


def batch_confidence(fixtures: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """为赛程列表中的每场比赛计算轻量把握度(仅依赖已有赔率字段).

    入参: 每个 fixture 至少含 id, odds_h, odds_d, odds_a; 可选 ou_line/ou_over/ou_under.
    返回: {id: {confidence_tier, confidence_score, confidence_factors, confidence_reasons}}
    复用 predict() 内部完整计算, 保证与 MatchAnalysisModal 的把握度完全同源.
    """
    out = {}
    for fx in fixtures:
        fid = fx.get("id")
        h, d, a = fx.get("odds_h"), fx.get("odds_d"), fx.get("odds_a")
        if not fid or not h or not d or not a:
            continue
        try:
            r = predict(
                home=fx.get("home", "主队"),
                away=fx.get("away", "客队"),
                h=float(h), d=float(d), a=float(a),
                ou_line=fx.get("ou_line"),
                ou_over=fx.get("ou_over"),
                ou_under=fx.get("ou_under"),
            )
            out[str(fid)] = {
                "confidence_tier": r.get("confidence_tier"),
                "confidence_score": r.get("confidence_score"),
                "confidence_factors": r.get("confidence_factors"),
                "confidence_reasons": r.get("confidence_reasons"),
            }
        except Exception:
            continue
    return out


if __name__ == "__main__":
    r = predict("洛杉矶银河", "洛杉矶FC", 2.5, 3.4, 2.6,
                ou_line=2.5, ou_over=1.93, ou_under=1.95,
                op_cs='[["1-1",8.3],["1-2",9.6],["2-1",11.0],["0-1",12.0],["2-2",12.5],["0-2",14.0],["1-0",14.0],["1-3",16.5],["2-0",18.5],["0-0",20.0]]')
    # 契约校验
    api = to_api_contract(r)
    eps = 1e-3
    assert api["prediction"] in ("H", "D", "A"), "prediction 必须为 H/D/A"
    prob_sum = api["probabilities"]["H"] + api["probabilities"]["D"] + api["probabilities"]["A"]
    assert abs(prob_sum - 1.0) < eps, f"H+D+A 概率和应=1, 实际={prob_sum}"
    # 校验 home/draw/away 与 H/D/A 镜像一致
    assert abs(api["probabilities"]["home"] - api["probabilities"]["H"]) < eps
    assert abs(api["probabilities"]["draw"] - api["probabilities"]["D"]) < eps
    assert abs(api["probabilities"]["away"] - api["probabilities"]["A"]) < eps
    assert isinstance(api["analysis"], dict) and "verdict" in api["analysis"], "analysis 须为含 verdict 的 dict"
    assert isinstance(api["combined_top"], list) and len(api["combined_top"]) > 0, "combined_top 须非空"
    print("=== ranked_predictor 自测通过 (契约校验 OK) ===")
    print("combined_top:", [(l, round(p, 3)) for l, p in r["combined_top"]])
    print("api.prediction:", api["prediction"], "| api.probabilities:", api["probabilities"])
    print("analysis.verdict:", api["analysis"]["verdict"])
    print(json.dumps(api, ensure_ascii=False, indent=2))
