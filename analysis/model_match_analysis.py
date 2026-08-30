# -*- coding: utf-8 -*-
"""滚球神器 · 决策智能体 (Phase 1, 按用户架构校正).

架构边界 (用户 2026-08-23 校正 + 2026-08-24 修订):
  - 给用户看的是 **决策智能体** 的产物: 它消费「模型的数据」(量化模型的盘口读数),
    输出 **决策 + 方案 + 合理比分**。
  - 本模块 = **纯确定性决策引擎**, **不调用任何本地 LLM (qwen3 等)**。
    决策由对齐模型概率 + 滚球盘信号 + 诚实锚推导, 可复现、零外部依赖、无推理延迟。
    用户明确要求: 不要再使用本地模型。

诚实锚 (禁诱导层):
  - 合理比分只由诚实锚推导: 1X2 去水最高概率方向 = 胜负方; OU 开盘去水隐含总球 = 总球数。
  - **严禁**用波胆(CS)定价推导比分: CS = 庄家诱导层, 最便宜波胆命中率仅约 9%。
  - 盘口真相源走 SSoT: analysis.live_goal_probe.get_opening_structure_diagnosis
    (kickoff 闸门, 仅开赛前最早快照, 无真实开盘不伪造)。

输出契约 (前端卡片直接消费):
  {
    ok, match_key, has_real_open,
    score_hint: {winner, winner_label, total, score, basis} | None,
    decision: "<一句话决策>" | None,
    plan: "<智能体决策/方案正文(markdown)>",
    model_data: {1x2, ou, ah, verdict},   # 模型的数据(供卡片展示)
    decision_engine: "deterministic",     # 纯规则引擎, 无本地模型
    error
  }
"""
import os
import re
import logging

logger = logging.getLogger("model_match_analysis")

DEFAULT_GQ_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db"
)

# 决策引擎: 纯确定性规则 (不调用任何本地 LLM)。
# 消费: 诚实锚比分(score_hint) + 对齐模型概率(aligned) + 滚球盘信号(live_sig) + 开盘结构(diag)。
# 设计: 滚球盘信号(实时模型)权重高于开盘结构; 置信度由概率差距与覆盖度决定; 不伪造漂移/跨庄证据。
_DECISION_FORMAT_HINT = None  # 占位, 保持模块导入稳定 (无需外部 prompt)


def _connect(db_path):
    import sqlite3
    return sqlite3.connect(db_path, timeout=30)


def _derive_score_from_diagnosis(diag):
    """用诚实锚由开盘结构诊断推导合理比分。无真实开盘返回 None。

    锚: (1) 1X2 去水三方向最高概率 = 胜负方(平局单独判);
        (2) OU 开盘去水隐含总球(ou_implied_total) = 总球数基准, 取整。禁 CS。
    """
    res = diag.get("result")
    be = diag.get("breakegg")
    if not res:
        return None
    ph, pd_, pa = res.get("p_home"), res.get("p_draw"), res.get("p_away")
    if None in (ph, pd_, pa):
        return None
    fav_p = max(ph, pd_, pa)
    if fav_p == pd_:
        winner = "draw"
    elif fav_p == ph:
        winner = "home"
    else:
        winner = "away"

    total_f = None
    if be and be.get("ou_implied_total") is not None:
        total_f = be["ou_implied_total"]
    elif be and be.get("x2_implied_total") is not None:
        total_f = be["x2_implied_total"]
    t = 1 if total_f is None else max(1, int(round(total_f)))

    label = {"home": "主胜", "draw": "平", "away": "客胜"}[winner]
    if winner == "draw":
        if t >= 2 and t % 2 == 0:
            score = f"{t // 2}-{t // 2}"
        elif t >= 2:
            score = "1-1"
        else:
            score = "0-0"
        basis = f"1X2平局锚(去水准概率最高) + OU隐含总球≈{total_f}"
    else:
        if t <= 1:
            w, l = 1, 0
        else:
            l = (t - 1) // 2
            w = t - l
            if w <= l:
                w = l + 1
        score = f"{w}-{l}" if winner == "home" else f"{l}-{w}"
        basis = f"1X2锚={label}(去水概率最高) + OU隐含总球≈{total_f}"
    return {"winner": winner, "winner_label": label, "total": t, "score": score, "basis": basis}


def _extract_decision_line(text):
    """从智能体输出中提取 '# 决策：...' 一句话作为卡片决策徽章。"""
    if not text:
        return None
    m = re.search(r"#\s*决策[:：]\s*(.+)", text)
    if m:
        return m.group(1).strip().split("\n")[0][:120]
    return None


def _format_live_block(sig):
    """把 live 信号格式化为可读文本块(供智能体/卡片消费)。"""
    if not sig:
        return ""
    L = []
    L.append(f"(滚球第{sig.get('minute')}分钟, 当前比分 {sig.get('score')})")
    if sig.get("1x2"):
        p = sig["1x2"]
        L.append(f"- 滚球1X2模型概率: 主胜 {p['p_home']} / 平 {p['p_draw']} / 客胜 {p['p_away']}")
    if sig.get("ou"):
        o = sig["ou"]
        over_tag = "大球" if o["p_over"] >= 0.5 else "小球"
        L.append(f"- 滚球大小球模型(线{o['line']}): 大球概率 {o['p_over']} → 倾向{over_tag}")
    return "\n".join(L)


def _derive_score_from_aligned(aligned):
    """用诚实锚由对齐结果(ModelRouter)推导合理比分。对齐失败返回 None。

    锚: (1) 对齐 verdict_1x2 (匹配模型 = ranked_predictor 经由 M1-M7 编排的 1X2 方向);
        (2) expected_total (OU 隐含总球基准), 取整。禁 CS。
    """
    if not aligned or not aligned.get("aligned"):
        return None
    verdict = aligned.get("verdict_1x2")
    if verdict not in ("主胜", "平局", "客胜"):
        return None
    winner = {"主胜": "home", "平局": "draw", "客胜": "away"}[verdict]

    total_f = aligned.get("expected_total")
    ou = aligned.get("ou") or {}
    if total_f is None:
        line = ou.get("line")
        direction = ou.get("direction")
        if line is not None and direction:
            d = str(direction)
            if ("大" in d) or (d == "over"):
                total_f = line + 0.5
            elif ("小" in d) or (d == "under"):
                total_f = line - 0.5
    t = 1 if total_f is None else max(1, int(round(total_f)))

    label = {"home": "主胜", "draw": "平", "away": "客胜"}[winner]
    if winner == "draw":
        if t >= 2 and t % 2 == 0:
            score = f"{t // 2}-{t // 2}"
        elif t >= 2:
            score = "1-1"
        else:
            score = "0-0"
        basis = f"匹配模型1X2锚=平局 + 对齐隐含总球≈{total_f}"
    else:
        if t <= 1:
            w, l = 1, 0
        else:
            l = (t - 1) // 2
            w = t - l
            if w <= l:
                w = l + 1
        score = f"{w}-{l}" if winner == "home" else f"{l}-{w}"
        basis = f"匹配模型1X2锚={label} + 对齐隐含总球≈{total_f}"
    return {"winner": winner, "winner_label": label, "total": t, "score": score, "basis": basis}


def _build_model_context(match_key, diag, aligned, current_score, current_minute, is_halftime, live_sig=None):
    """把「对齐后的模型数据」整理成智能体可读的上下文。live_sig=滚球盘信号(可选)。"""
    res = diag.get("result") or {}
    be = diag.get("breakegg") or {}
    L = []
    L.append(f"比赛: {match_key}")
    if current_score and current_score != "0-0":
        L.append(f"当前比分/分钟: {current_score} / {current_minute}'" + (" (中场休息)" if is_halftime else ""))
    L.append("")
    L.append("【模型的数据(已对齐到匹配模型 = ranked_predictor / M1-M7 任务制编排)】")

    # 1X2: 对齐 verdict + 概率 (匹配模型结论)
    p1 = aligned.get("probs_1x2") or {}
    L.append(f"- 1X2 匹配模型结论: {aligned.get('verdict_1x2')} "
             f"(主胜 {p1.get('主胜')} / 平 {p1.get('平局')} / 客胜 {p1.get('客胜')})")
    # 多模型分歧校准
    rec = aligned.get("reconcile") or {}
    if rec.get("conflict"):
        L.append(f"  → 多模型分歧: {rec.get('note')} (主模型融合结论已胜出, 仅供参考)")
    # OU
    ou = aligned.get("ou") or {}
    if ou.get("line") is not None:
        L.append(f"- 大小球: 线 {ou.get('line')} "
                 f"(方向 {ou.get('direction')}, 大 {ou.get('p_over')} / 小 {ou.get('p_under')})")
    if aligned.get("expected_total") is not None:
        L.append(f"  → 隐含总球≈{aligned.get('expected_total')}")
    # AH
    ah = aligned.get("ah")
    if ah:
        L.append(f"- 让球(AH): {ah}")
    # 操盘手意图
    oi = aligned.get("operator_intent")
    if oi:
        L.append(f"- 操盘手意图: {oi}")
    # 开盘结构残差 (该场特异)
    L.append(f"- 开盘结构残差: {be.get('break_tendency') or '无特异'} "
             f"(structure_residual={be.get('structure_residual')})")
    if diag.get("verdict"):
        L.append(f"逐场信号: {diag.get('verdict')}")
    # 滚球盘信号 (live 模型, 决策智能体直接消费)
    if live_sig and (live_sig.get("1x2") or live_sig.get("ou")):
        L.append("")
        L.append("【滚球盘信号(实时模型, 当前盘口推理)】")
        L.append(_format_live_block(live_sig))
    return "\n".join(L)


def _build_decision(match_key, diag, aligned, current_score, current_minute, is_halftime, live_sig, score_hint,
                    injury_note=None):
    """纯确定性决策引擎 (不调用任何本地 LLM)。

    消费: 诚实锚比分(score_hint) + 对齐模型概率(aligned) + 滚球盘信号(live_sig) + 开盘结构(diag)。
    规则:
      - 1X2 方向: 滚球盘信号优先(实时), 否则对齐/开盘去水概率; 取最高概率方向。
      - 大小球: 滚球 OU 模型优先(原始赔率不去水), 否则对齐 OU 方向。
      - 置信度: 由最高概率 + 与次高差决定 (高/中/低)。
      - 诚实边界: 不伪造开盘→收盘漂移/跨庄分歧; 无真实开盘已在调用方拦截。
    返回: (decision_一句话, plan_markdown)
    """
    res = diag.get("result") or {}
    be = diag.get("breakegg") or {}
    p1 = aligned.get("probs_1x2") or {}

    # ---- 1X2 方向概率来源: 滚球优先 ----
    if live_sig and live_sig.get("1x2"):
        lp = live_sig["1x2"]
        ph, pd_, pa = lp["p_home"], lp["p_draw"], lp["p_away"]
        x2_src = "滚球盘信号(live 1X2 模型)"
    else:
        ph, pd_, pa = p1.get("主胜"), p1.get("平局"), p1.get("客胜")
        if None in (ph, pd_, pa):
            ph, pd_, pa = res.get("p_home"), res.get("p_draw"), res.get("p_away")
        x2_src = "开幕盘对齐(ranked_predictor)"
    probs = {}
    for k, v in (("主胜", ph), ("平", pd_), ("客胜", pa)):
        if v is not None:
            probs[k] = float(v)
    if probs:
        best = max(probs, key=probs.get)
        best_p = probs[best]
        sp = sorted(probs.values(), reverse=True)
        gap = (sp[0] - sp[1]) if len(sp) >= 2 else best_p
    else:
        best, best_p, gap = None, 0.0, 0.0

    # ---- 置信度 ----
    if best_p >= 0.6 and gap >= 0.15:
        conf = "高"
    elif best_p >= 0.5 and gap >= 0.08:
        conf = "中"
    else:
        conf = "低"

    # ---- 大小球方向 ----
    ou_dir = None
    ou_line = None
    ou_over_p = None
    ou_src = None
    if live_sig and live_sig.get("ou"):
        o = live_sig["ou"]
        ou_line = o["line"]
        ou_over_p = o["p_over"]
        ou_dir = "大球" if ou_over_p >= 0.5 else "小球"
        ou_src = "滚球盘信号(live OU 模型, 原始赔率)"
    else:
        ou = aligned.get("ou") or {}
        ou_line = ou.get("line")
        d = ou.get("direction")
        if d:
            s = str(d)
            ou_dir = "大球" if ("大" in s or s == "over") else ("小球" if ("小" in s or s == "under") else None)
        ou_src = "开幕盘对齐"

    # ---- 让球 ----
    ah = aligned.get("ah")
    ah_txt = (str(ah) if ah else "无")

    # ---- 决策方向一句话 ----
    if best is None:
        decision = "观望 (无有效 1X2 概率) （置信：低）"
    elif conf == "低":
        x2_dir = {"主胜": "主胜", "客胜": "客胜", "平": "平局"}.get(best, best)
        decision = f"观望（{x2_dir}概率不占优, 置信：低）"
    else:
        x2_dir = {"主胜": "主胜", "客胜": "客胜", "平": "平局"}.get(best, best)
        decision = f"建仓方向: {x2_dir}（置信：{conf}）"

    # ---- 注码 / 风控 ----
    if conf == "高":
        stake = "半凯利建仓 (主方向概率占优且差幅充足)"
    elif conf == "中":
        stake = "小注 (概率占优但差幅有限, 控仓)"
    else:
        stake = "不建仓 / 仅观望 (概率不占优或证据不足)"

    if live_sig and live_sig.get("1x2"):
        floor = max(0.4, round(best_p - 0.15, 2))
        risk = f"以滚球盘信号为准; 若实时主方向概率跌破 {floor} 则止损离场"
    else:
        risk = "单庄开盘无收盘漂移/跨庄分歧, 置信有限; 触发条件: 临场水位异动或赛果背离"

    # ---- 依据解读 ----
    basis_lines = []
    if probs:
        basis_lines.append(
            f"1X2：{x2_src} → 主胜 {ph} / 平 {pd_} / 客胜 {pa}; 最优方向 **{best}** "
            f"(p={round(best_p, 3)}, 差幅 {round(gap, 3)})"
        )
    if ou_dir:
        otxt = f"p_over={ou_over_p}" if ou_over_p is not None else ""
        basis_lines.append(f"大小球：{ou_src} → 线 {ou_line} 倾向 **{ou_dir}** {otxt}")
    basis_lines.append(f"让球：{ah_txt}")
    rec = aligned.get("reconcile") or {}
    if rec.get("conflict"):
        basis_lines.append(f"多模型分歧: {rec.get('note')} (以主模型融合结论为准)")
    # 2026-08-28: 伤病调节信号 (乐鱼扩展 match_meta, 非训练特征, 只做决策文本提示)
    if injury_note:
        basis_lines.append(f"伤病: {injury_note}")

    if score_hint:
        sh_txt = (f"{score_hint['score']}（{score_hint['winner_label']}） "
                  f"[诚实锚: 1X2+OU, {score_hint.get('basis', '')}]")
    else:
        sh_txt = "无 (无真实开盘/对齐失败)"

    plan = [
        f"# 决策：{decision}",
        "## 方案",
        f"- 方向：{'1X2 ' + best if best else '观望'} / 大小球 {ou_dir or '观望'} / 让球 {ah_txt}",
        f"- 注码：{stake}",
        f"- 风控：{risk}",
        f"- 合理比分：{sh_txt}",
        "## 依据（模型数据解读）",
    ] + [f"- {b}" for b in basis_lines]
    return decision, "\n".join(plan)


def _infer_winner_from_score(score):
    """从比分推断胜负方: '2:0'/'2-0' → home, '0:2' → away, '1:1' → draw。"""
    try:
        s = str(score).replace(':', '-')
        mh, ma = (int(x) for x in s.split('-')[:2])
    except Exception:
        return 'home'
    if mh > ma:
        return 'home'
    if ma > mh:
        return 'away'
    return 'draw'


def _parse_score_safe(s):
    """安全解析比分 'a-b' / 'a:b' → (int,int), 失败 (0,0)。"""
    if not s:
        return (0, 0)
    if not s:
        return (0, 0)
    s = str(s).strip()
    for sep in ["-", ":"]:
        if sep in s:
            try:
                a, b = s.split(sep)
                return (int(a), int(b))
            except Exception:
                continue
    return (0, 0)


_LIVE_MODELS = None  # (live_1x2, live_ou)

# 滚球 1X2 混合权重: w*live + (1-w)*去水
# 来源 lock_w_holdout.py 时序 holdout (kickoff>=2026-08-14, n=14465): AUC 0.8217 严格优于两端, LogLoss 最低
W_INPLAY_LIVE = 0.6


def _load_live_models():
    """懒加载滚球 live 模型 (data/live_{1x2,ou}_model.joblib)。失败返回 (None,None)。"""
    global _LIVE_MODELS
    if _LIVE_MODELS is not None:
        return _LIVE_MODELS
    try:
        import joblib, os
        base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        m1 = joblib.load(os.path.join(base, "live_1x2_model.joblib"))
        m2 = joblib.load(os.path.join(base, "live_ou_model.joblib"))
        _LIVE_MODELS = (m1, m2)
        logger.info("[decision_agent] live 滚球模型已加载")
    except Exception as e:
        logger.warning(f"[decision_agent] live 模型加载失败: {e}")
        _LIVE_MODELS = (None, None)
    return _LIVE_MODELS


def _compute_live_signal(con, match_key, minute, score):
    """滚球盘信号: 取当前 in-play 盘口 → live 1X2 / OU 概率。无盘返回 None。

    特征口径与训练严格一致:
      1X2: [minute/90, sc_h, sc_a, lead, de-vig p_h, p_d, p_a]
      OU  : [minute/90, sc_h, sc_a, lead, ou_line, 1/over, 1/under]  (原始赔率, 不去水)
    """
    try:
        from analysis.live_goal_probe import _current_inplay_odds, _dewater_1x2
        from analysis.live_rollball_features import build_ou_features, build_1x2_features
    except Exception as e:
        logger.warning(f"[decision_agent] live_goal_probe 导入失败: {e}")
        return None
    m1, m2 = _load_live_models()
    if m1 is None or m2 is None:
        return None
    cur = _current_inplay_odds(con, match_key, minute)
    if not cur:
        return None
    try:
        import numpy as np
        sh, sa = _parse_score_safe(score)
        minute_norm = max(1, min(95, minute)) / 90.0
        lead = sh - sa
        sig = {"minute": int(minute), "score": score, "available": True, "1x2": None, "ou": None}
        if cur.get("x2"):
            h, d, a = cur["x2"]
            p1 = _dewater_1x2(h, d, a)
            if p1:
                X = np.array([build_1x2_features(minute, sh, sa, p1[0], p1[1], p1[2])], dtype=float)
                p = m1.predict_proba(X)[0]
                # 优化混合: w*live + (1-w)*去水 (时序holdout锁定 w=0.6, AUC 0.8217 严格优于两端)
                w = W_INPLAY_LIVE
                blended = [w * p[i] + (1 - w) * p1[i] for i in range(3)]
                ssum = sum(blended)
                blended = [x / ssum for x in blended]
                sig["1x2"] = {"p_home": round(float(blended[0]), 3), "p_draw": round(float(blended[1]), 3),
                              "p_away": round(float(blended[2]), 3)}
        if cur.get("ou"):
            line, ov, un = cur["ou"]
            if ov and un and ov > 1.0 and un > 1.0:
                X = np.array([build_ou_features(minute, sh, sa, line, ov, un)], dtype=float)
                p = m2.predict_proba(X)[0]
                # 保留真实 over/under 赔率, 供 consensus 端点 devig 算 OU EV 代理 (诚实边界: 不丢弃市场赔率)
                sig["ou"] = {"line": line, "p_over": round(float(p[1]), 3),
                             "over_odds": round(float(ov), 3), "under_odds": round(float(un), 3)}
        if sig["1x2"] is None and sig["ou"] is None:
            return None
        return sig
    except Exception as e:
        logger.warning(f"[decision_agent] live 信号推理失败: {e}")
        return None


def analyze_match_with_model(match_key, current_score="0-0", current_minute=0,
                             is_halftime=False, db_path=None):
    """决策智能体主入口: 开盘提取 → 匹配模型对齐 → 诚实锚派生比分 → 智能体出决策。

    对齐层 (2026-08-24): 所有分析经 pipeline.predictors.model_router.ModelRouter,
    单一 SSoT 编排 (ranked_predictor / M1-M7), 由 ConsistencyValidator 校准多模型分歧。
    返回 dict (见模块 docstring 输出契约)。
    """
    db_path = db_path or DEFAULT_GQ_PATH
    out = {
        "ok": False, "match_key": match_key, "has_real_open": False,
        "score_hint": None, "decision": None, "plan": "",
        "model_data": None, "decision_engine": "deterministic", "error": None,
    }
    if not match_key:
        out["error"] = "match_key 为空"
        return out

    con = None
    try:
        con = _connect(db_path)
        from analysis.live_goal_probe import (
            get_opening_structure_diagnosis, _open_1x2_from_snapshots,
            _open_total_from_snapshots, _open_ah_from_snapshots,
            _current_inplay_odds,
        )
        diag = get_opening_structure_diagnosis(con, match_key)
        # 原始赔率 (ModelRouter 需要 h/d/a; 诊断只暴露去水概率)
        h, d, a = _open_1x2_from_snapshots(con, match_key)
        ou_line, _ou_T = _open_total_from_snapshots(
            con, match_key, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
        ah_line, ah_h, ah_a = _open_ah_from_snapshots(con, match_key)
    except Exception as e:
        logger.error(f"[decision_agent] 开盘提取失败: {e}")
        out["error"] = f"开盘提取失败: {e}"
        if con:
            con.close()
        return out

    out["has_real_open"] = bool(diag.get("has_real_open"))
    res = diag.get("result") or {}
    be = diag.get("breakegg") or {}

    # ── 匹配模型对齐 (SSoT: ranked_predictor / M1-M7) ──
    try:
        from pipeline.predictors.model_router import ModelRouter
        aligned = ModelRouter.analyze(
            home=match_key.split(" vs ")[0] if " vs " in match_key else None,
            away=match_key.split(" vs ")[1] if " vs " in match_key else None,
            h=float(h), d=float(d), a=float(a),
            ou_line=(float(ou_line) if ou_line is not None else None),
            ah_line=(float(ah_line) if ah_line is not None else None),
            ah_home=(float(ah_h) if ah_h is not None else None),
            ah_away=(float(ah_a) if ah_a is not None else None),
        )
    except Exception as e:
        logger.warning(f"[decision_agent] 对齐层异常, 退回诚实锚: {e}")
        aligned = None

    # 2026-08-28 全局优化: 比分识别升级为【三盘交叉 + 滚球验证】(cross_score):
    # 初盘 1X2+OU+AH 三市场交叉拟合 λ/μ → 全比分分布 → 滚球条件化(当前比分/分钟)
    # → 滚球 OU 漂移验证。有初盘三盘时优先, 无则退回原对齐/诊断诚实锚。
    score_hint = None
    try:
        from pipeline.cross_score import derive_score_cross
        _cs = derive_score_cross(con, match_key, current_score, current_minute)
        if _cs and _cs.get('found') and _cs.get('score'):
            # 2026-08-30: 方向直出 — 滚球态用领先方先验(干净频率表)⊕即时盘, 不再从
            # 比分top反推(会被DB匹配分布稀释); 无直出值时回退比分推断。
            _win = _cs.get('winner') or _infer_winner_from_score(_cs['score'])
            score_hint = {
                'winner': _win, 'winner_label': {'home': '主胜', 'draw': '平', 'away': '客胜'}.get(_win, '主胜'),
                'total': None, 'score': str(_cs['score']).replace(':', '-'),
                # 2026-08-28: 双比分推荐 — ①初盘+即时结构 ②滚球修正
                'score_opening': str(_cs['score_opening']).replace(':', '-') if _cs.get('score_opening') else None,
                'opening_basis': _cs.get('opening_basis'),
                'basis': _cs.get('basis', ''),
                'top3': _cs.get('top3'),
                'top3_opening': _cs.get('top3_opening'),
                'roll_verification': _cs.get('roll_verification'),
                # 2026-08-29: 与实时比分的方向冲突标记 (波胆反向根因修复 Fix-3)。
                #   opening_conflict: 初盘结论方向与当前比分领先方相反
                #   roll_conflict:    主推比分方向与当前比分领先方相反
                #   前端据此区分"真反向"(报警) 与"模型推终场≠当前比分"(正常, 不报警)。
                'opening_conflict': bool(_cs.get('opening_conflict')),
                'roll_conflict': bool(_cs.get('roll_conflict')),
                # 2026-08-29 方向3: 领先方先验校正说明 (None=未生效, 如赛前/平局/样本不足)
                'lead_prior_note': _cs.get('lead_prior_note'),
            }
    except Exception:
        score_hint = None
    if score_hint is None:
        score_hint = _derive_score_from_aligned(aligned) if aligned else _derive_score_from_diagnosis(diag)
    out["score_hint"] = score_hint
    # cross_score 的三盘依据并入决策文本(供"依据"段展示)
    try:
        if score_hint and score_hint.get('basis') and '三盘' in (score_hint.get('basis') or ''):
            out['score_hint_basis'] = score_hint.get('basis')
    except Exception:
        pass

    # ── 滚球盘信号 (live 模型): 仅在滚球场景(current_minute>0)消费 ──
    live_sig = None
    if current_minute and current_minute > 0:
        live_sig = _compute_live_signal(con, match_key, current_minute, current_score)

    # 模型的数据 (供卡片展示): 对齐结果优先, 开盘结构兜底, 滚球信号附加
    out["model_data"] = {
        "matched_model": (aligned.get("matched_model") if aligned else "诚实锚(对齐层未生效)"),
        "1x2": {
            "verdict": (aligned.get("verdict_1x2") if aligned else res.get("fav_1x2")),
            "p_home": (aligned.get("probs_1x2", {}).get("主胜") if aligned else res.get("p_home")),
            "p_draw": (aligned.get("probs_1x2", {}).get("平局") if aligned else res.get("p_draw")),
            "p_away": (aligned.get("probs_1x2", {}).get("客胜") if aligned else res.get("p_away")),
            # 去水开盘公平概率 (SSoT: 开盘结构诊断的去水结果), 供 discrepancy 字段对比模型概率
            "fair_p_home": res.get("p_home"),
            "fair_p_draw": res.get("p_draw"),
            "fair_p_away": res.get("p_away"),
        },
        "ou": {"open_line": be.get("ou_open_line"), "implied_total": be.get("ou_implied_total"),
               "direction": (aligned.get("ou", {}).get("direction") if aligned else None),
               "break_tendency": be.get("break_tendency")},
        "ah": {"line": res.get("ah_line"), "fav_side": res.get("ah_fav_side"),
               "fav_prob": res.get("ah_fav_prob"), "consistency": res.get("fav_consistency")},
        "reconcile": (aligned.get("reconcile") if aligned else None),
        "verdict": diag.get("verdict"),
        "live": live_sig,
    }

    if not diag.get("has_real_open") or score_hint is None:
        out["ok"] = True
        out["decision"] = "无真实开盘, 不决策"
        plan = "本场无真实开盘赔率(或开赛前快照缺失), 无法推导合理比分与结构。请以当前 live 盘口另行判读。"
        if live_sig and (live_sig.get("1x2") or live_sig.get("ou")):
            plan += "\n\n【滚球盘信号(实时模型)】\n" + _format_live_block(live_sig)
        out["plan"] = plan
        if con:
            con.close()
        return out

    # 决策引擎: 纯确定性规则 (不调用本地 LLM)。消费对齐概率 + 滚球盘信号 + 诚实锚。
    # 2026-08-28: 伤病调节信号 — 乐鱼扩展 content_collector 采集的 match_meta.injuries,
    # 作为"实时情报"注入决策文本(非训练特征, 不重训模型; 有则提示, 无则跳过)。
    injury_note = None
    if con is not None:
        try:
            _r = con.execute(
                "SELECT injuries_home FROM match_meta WHERE match_key=? AND injuries_home!=''",
                (match_key,),
            ).fetchone()
            if _r and _r[0]:
                import json as _j
                _inj = _j.loads(_r[0]) if isinstance(_r[0], str) else None
                if isinstance(_inj, dict):
                    parts = []
                    for side_key, side_label in (("1", "主队"), ("2", "客队")):
                        items = _inj.get(side_key)
                        if isinstance(items, list) and items:
                            names = []
                            for it in items[:5]:
                                if isinstance(it, dict) and it.get('playerName'):
                                    names.append(
                                        f"{it.get('playerName')}({it.get('positionName') or '?'}"
                                        f"{'|' + it.get('reason') if it.get('reason') else ''})"
                                    )
                            if names:
                                parts.append(f"{side_label}缺 {len(names)} 人: {'、'.join(names)}")
                    if parts:
                        injury_note = "; ".join(parts) + " [实时采集, 仅供决策参考, 非训练特征]"
        except Exception:
            injury_note = None
    decision, plan = _build_decision(
        match_key, diag, aligned, current_score, current_minute, is_halftime, live_sig, score_hint,
        injury_note=injury_note,
    )
    out["decision"] = decision
    out["plan"] = plan
    out["decision_engine"] = "deterministic"

    out["ok"] = True
    if con:
        con.close()
    return out


if __name__ == "__main__":
    import sys
    mk = sys.argv[1] if len(sys.argv) > 1 else None
    if not mk:
        print("usage: python model_match_analysis.py '<match_key>'")
        sys.exit(1)
    r = analyze_match_with_model(mk)
    print("ok=", r["ok"], "has_real_open=", r["has_real_open"], "engine=", r["decision_engine"])
    print("decision=", r["decision"])
    print("score_hint=", r["score_hint"])
    print("plan=", r["plan"][:800])
