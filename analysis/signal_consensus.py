# -*- coding: utf-8 -*-
"""信号一致性 / 裁决层 (C 完整档核心).

把滚球神器页面多路独立信号(1X2 / OU / AH / 操盘手 / 决策智能体)归一为统一三元词汇,
产出 4 个字段, 供前端「决策仲裁卡」消费, 消除"多卡互相矛盾"的体感:

  - signal_consensus    : {agreement, conflicts[], primary_signal, signals[]}
  - discrepancy         : 模型 p_true vs 去水 p_fair 的 EV 代理 (报告 §3.1)
  - closing_line_value  : 开盘→实时盘口漂移 (edge 代理, 报告 §2.3/§5.3), 带双重语义警示
  - confidence_interval : 各信号粗略置信带 (明确标注非统计 CI, 需 bootstrap 校准)

诚实边界 (对齐 v7.4 报告 + 铁律 IR-17/IR-18/IR-30):
  - 不伪造证据; 数据缺失时对应字段 available=false + 说明, 不编造数字.
  - discrepancy 仅在同时有模型概率与去水平盘概率时计算; 否则 available=false.
  - closing_line_value 仅在有实时盘口对照开盘时计算, 且明确"漂移具双重语义(赔付管理 vs 热度示警)".
  - confidence_interval 为粗略带 (基于与次高概率的差距), 非统计置信区间, 明确标注.
"""
import logging

logger = logging.getLogger("signal_consensus")

# ── 词汇归一表 ──
_X12_VOCAB = {
    "home": "home", "主胜": "home", "主队胜": "home", "home_win": "home", "主": "home",
    "draw": "draw", "平局": "draw", "平": "draw", "tie": "draw",
    "away": "away", "客胜": "away", "客队胜": "away", "away_win": "away", "客": "away",
}
_OU_VOCAB = {
    "over": "over", "大球": "over", "大": "over", "over_win": "over",
    "under": "under", "小球": "under", "小": "under", "under_win": "under",
}
_X12_LABEL = {"home": "主胜", "draw": "平局", "away": "客胜"}
_OU_LABEL = {"over": "大球", "under": "小球"}


def _norm_x12(v):
    if v is None:
        return None
    return _X12_VOCAB.get(str(v).strip(), None)


def _norm_ou(v):
    if v is None:
        return None
    return _OU_VOCAB.get(str(v).strip(), None)


def _parse_x12_from_text(text):
    """从操盘手 verdict 文本里挖 1X2 方向词。返回 home/draw/away 或 None。"""
    if not text:
        return None
    t = str(text)
    if "主胜" in t or "主队胜" in t:
        return "home"
    if "客胜" in t or "客队胜" in t:
        return "away"
    if "平局" in t or t.strip() == "平" or "平局" in t:
        return "draw"
    return None


def _parse_ou_from_text(text):
    if not text:
        return None
    t = str(text)
    if "大球" in t or "看大" in t or "大球区" in t:
        return "over"
    if "小球" in t or "看小" in t or "小球区" in t:
        return "under"
    return None


def _conf_from_probs(probs):
    """由概率字典推断 0-1 置信: 最高概率 + 与次高差幅。"""
    if not probs:
        return 0.0
    vals = sorted([v for v in probs.values() if isinstance(v, (int, float)) and v == v], reverse=True)
    if not vals:
        return 0.0
    best = vals[0]
    gap = (vals[0] - vals[1]) if len(vals) >= 2 else best
    if best >= 0.6 and gap >= 0.15:
        return 0.9
    if best >= 0.5 and gap >= 0.08:
        return 0.65
    if best >= 0.4:
        return 0.4
    return 0.2


# ───────────────────────────────────────────────────────────
# 信号抽取
# ───────────────────────────────────────────────────────────
def extract_signals(analyze_out, operator_card, ou_decision=None):
    """从三个端点返回里抽取归一化信号列表。每个信号:
    {source, axis, value, label, confidence, detail}
    axis ∈ {1X2, OU}; value 归一为 home/draw/away 或 over/under。
    """
    signals = []
    if not analyze_out:
        return signals

    md = analyze_out.get("model_data") or {}
    x12 = md.get("1x2") or {}
    ou = md.get("ou") or {}
    ah = md.get("ah") or {}
    sh = analyze_out.get("score_hint") or {}
    live = md.get("live") or {}

    # 1) 决策智能体 · 诚实锚比分 winner (1X2)
    if sh.get("winner"):
        w = _norm_x12(sh["winner"])
        if w:
            signals.append({
                "source": "决策智能体·诚实锚", "axis": "1X2", "value": w,
                "label": f"{_X12_LABEL[w]} (合理比分 {sh.get('score','')})",
                "confidence": 0.7, "detail": sh.get("basis", ""),
            })

    # 2) 模型 1X2 verdict + 概率
    v = _norm_x12(x12.get("verdict"))
    probs = {k: x12.get(k) for k in ("p_home", "p_draw", "p_away") if x12.get(k) is not None}
    if v:
        signals.append({
            "source": "匹配模型 1X2", "axis": "1X2", "value": v,
            "label": f"{_X12_LABEL[v]} (p={x12.get(('p_'+ ('home' if v=='home' else 'draw' if v=='draw' else 'away')))})".replace("p=None", "p=?"),
            "confidence": _conf_from_probs(probs), "detail": "",
        })
    elif probs:
        # verdict 缺失但有概率 → 由最高概率定方向
        best = max(probs, key=probs.get)
        signals.append({
            "source": "匹配模型 1X2", "axis": "1X2", "value": best,
            "label": f"{_X12_LABEL[best]} (p={round(probs[best],3)})",
            "confidence": _conf_from_probs(probs), "detail": "",
        })

    # 3) AH 让球 fav_side (视为 1X2 谁被看好)
    fs = _norm_x12(ah.get("fav_side"))
    if fs is None and ah.get("fav_side"):
        fs = _norm_x12(str(ah["fav_side"]).replace("主", "home").replace("客", "away"))
    if fs:
        cons_raw = ah.get("consistency")
        cons_label = {"consistent": "一致", "inconsistent": "不一致", "high": "高", True: "一致", False: "不一致"}.get(cons_raw, cons_raw if cons_raw is not None else "?")
        signals.append({
            "source": "让球 AH", "axis": "1X2", "value": fs,
            "label": f"{_X12_LABEL[fs]} (盘口倾向, 一致度 {cons_label})",
            "confidence": 0.6 if cons_raw in (True, "一致", "high", "consistent") else 0.45,
            "detail": f"line={ah.get('line')}",
        })

    # 4) OU 方向
    od = _norm_ou(ou.get("direction"))
    if od:
        signals.append({
            "source": "大小球 OU", "axis": "OU", "value": od,
            "label": f"{_OU_LABEL[od]} (开盘线 {ou.get('open_line')}, 隐含总球 {ou.get('implied_total')})",
            "confidence": 0.6, "detail": ou.get("break_tendency") or "",
        })

    # 5) 操盘手卡
    if operator_card:
        oc_v = operator_card.get("verdict")
        oc_v_short = (oc_v or "")[:40] + ("…" if oc_v and len(oc_v) > 40 else "")
        oc_dir = _parse_x12_from_text(oc_v)
        if oc_dir:
            signals.append({
                "source": "操盘手结论", "axis": "1X2", "value": oc_dir,
                "label": f"{_X12_LABEL[oc_dir]} (verdict: {oc_v_short})",
                "confidence": float(operator_card.get("confidence") or 0.5),
                "detail": f"decision={operator_card.get('decision')}, trap={operator_card.get('trap_score')}",
            })
        # 操盘手证据里可能出现的 OU 方向
        for ev in (operator_card.get("evidence") or []):
            ou_dir = _parse_ou_from_text(ev)
            if ou_dir:
                signals.append({
                    "source": "操盘手结论·证据", "axis": "OU", "value": ou_dir,
                    "label": f"{_OU_LABEL[ou_dir]} (来自证据: {ev})",
                    "confidence": 0.4, "detail": "",
                })

    # 6) live 实时盘口信号 (滚球场景)
    if live and isinstance(live, dict):
        lx = live.get("1x2")
        if lx:
            _lk = {"p_home": "home", "p_draw": "draw", "p_away": "away"}
            lp = {_lk[k]: lx.get(k) for k in ("p_home", "p_draw", "p_away")
                  if lx.get(k) is not None and _lk.get(k)}
            if lp:
                b = max(lp, key=lp.get)
                signals.append({
                    "source": "滚球实时盘口 1X2", "axis": "1X2", "value": b,
                    "label": f"{_X12_LABEL[b]} (live p={round(lp[b],3)})",
                    "confidence": _conf_from_probs(lp), "detail": f"minute={live.get('minute')}",
                })
        lo = live.get("ou")
        if lo and lo.get("p_over") is not None:
            lod = "over" if lo["p_over"] >= 0.5 else "under"
            signals.append({
                "source": "滚球实时盘口 OU", "axis": "OU", "value": lod,
                "label": f"{_OU_LABEL[lod]} (live p_over={lo['p_over']}, 线 {lo.get('line')})",
                "confidence": 0.6, "detail": "",
            })

    # 7) OU 决策端点 (含 devig 证据闸门)
    if ou_decision:
        for side in ("over", "under"):
            s = ou_decision.get(side) or {}
            if s.get("verdict"):
                signals.append({
                    "source": f"OU破蛋决策·{side}", "axis": "OU", "value": side,
                    "label": f"{_OU_LABEL[side]} (verdict: {s.get('verdict')}, div={s.get('divergence')})",
                    "confidence": 0.7 if ou_decision.get("bettable") else 0.4,
                    "detail": f"fair={s.get('fair_prob')}, implied={s.get('implied_prob')}",
                })

    return signals


def _axis_consensus(signals, axis):
    """对单一 axis 计算: 多数方向 + 冲突列表。"""
    vals = [s for s in signals if s["axis"] == axis and s["value"] is not None]
    if not vals:
        return None
    # 多数方向 (按置信加权计数)
    weight = {}
    for s in vals:
        weight[s["value"]] = weight.get(s["value"], 0) + (0.5 + s["confidence"])
    majority = max(weight, key=weight.get)
    conflicts = []
    for s in vals:
        if s["value"] != majority:
            conflicts.append({
                "source": s["source"], "axis": axis,
                "value": s["value"], "value_label": (s["label"] or s["source"]),
                "majority": majority,
            })
    return {
        "majority": majority,
        "majority_label": (_X12_LABEL.get(majority) or _OU_LABEL.get(majority)),
        "n_signals": len(vals),
        "conflicts": conflicts,
    }


def build_signal_consensus(analyze_out, operator_card, ou_decision=None):
    """主入口: 返回 {signal_consensus, discrepancy, closing_line_value, confidence_interval}。"""
    signals = extract_signals(analyze_out, operator_card, ou_decision)

    x12 = _axis_consensus(signals, "1X2")
    ou = _axis_consensus(signals, "OU")

    # ── agreement 等级 ──
    axes = [a for a in (x12, ou) if a]
    if not axes:
        agreement = "无信号"
    else:
        max_conflict = max((len(a["conflicts"]) for a in axes), default=0)
        n_axes = len(axes)
        if max_conflict == 0:
            agreement = "一致"
        elif max_conflict == 1 and n_axes >= 1:
            agreement = "部分分歧"
        else:
            agreement = "严重分歧"

    # ── primary_signal: 跨轴最高置信且属多数的信号 ──
    primary = None
    for s in sorted(signals, key=lambda x: -x["confidence"]):
        ax = _axis_consensus(signals, s["axis"])
        if ax and ax["majority"] == s["value"]:
            primary = {"source": s["source"], "axis": s["axis"], "value": s["value"],
                       "label": s["label"], "confidence": round(s["confidence"], 2)}
            break

    conflicts_flat = []
    for a in axes:
        for c in a["conflicts"]:
            conflicts_flat.append(c)

    signal_consensus = {
        "available": bool(signals),
        "agreement": agreement,
        "primary_signal": primary,
        "n_signals": len(signals),
        "axes": {
            "1X2": ({"majority": x12["majority"], "majority_label": x12["majority_label"],
                     "n": x12["n_signals"], "n_conflict": len(x12["conflicts"])} if x12 else None),
            "OU": ({"majority": ou["majority"], "majority_label": ou["majority_label"],
                    "n": ou["n_signals"], "n_conflict": len(ou["conflicts"])} if ou else None),
        },
        "conflicts": conflicts_flat,
        "signals": signals,
    }

    # ── discrepancy: 模型 p_true vs 去水 p_fair (1X2) ──
    discrepancy = _build_discrepancy(analyze_out, ou_decision)

    # ── closing_line_value: 开盘→实时盘口漂移 ──
    clv = _build_clv(analyze_out)

    # ── confidence_interval: 粗略置信带 ──
    ci = _build_ci(analyze_out, operator_card)

    return {
        "signal_consensus": signal_consensus,
        "discrepancy": discrepancy,
        "closing_line_value": clv,
        "confidence_interval": ci,
    }


def _build_discrepancy(analyze_out, ou_decision):
    """模型概率 vs 去水平盘概率。1X2 用 analyze 的 model vs fair; OU 用 ou_decision 的 fair vs implied。"""
    out = {"available": False, "note": "", "1x2": None, "ou": None}
    md = (analyze_out or {}).get("model_data") or {}
    x12 = md.get("1x2") or {}

    model_probs = {k: x12.get(k) for k in ("p_home", "p_draw", "p_away") if x12.get(k) is not None}
    fair_probs = {k: x12.get("fair_" + k) for k in ("p_home", "p_draw", "p_away") if x12.get("fair_" + k) is not None}
    if model_probs and fair_probs:
        # 取模型最高方向 (键为 p_home/p_draw/p_away, 映射为 home/draw/away)
        _pk = {"p_home": "home", "p_draw": "draw", "p_away": "away"}
        best_dir = max(model_probs, key=model_probs.get)
        best_norm = _pk.get(best_dir, best_dir)
        mp = model_probs[best_dir]
        fp = fair_probs.get(best_dir)
        if fp is not None:
            gap = round(float(mp) - float(fp), 4)
            out["1x2"] = {
                "direction": _X12_LABEL.get(best_norm, best_norm),
                "model_p": round(float(mp), 4),
                "fair_p": round(float(fp), 4),
                "gap": gap,
                "ev_lean": ("模型高于去水平盘(潜在 +EV, 需证据闸门复核)" if gap > 0.03
                            else "模型≈去水平盘(无显著 edge)" if abs(gap) <= 0.03
                            else "模型低于去水平盘(盘口已消化)"),
            }
            out["available"] = True

    if ou_decision:
        ov = ou_decision.get("over") or {}
        un = ou_decision.get("under") or {}
        fp = ov.get("fair_prob")
        ip = ov.get("implied_prob")
        if fp is not None and ip is not None:
            out["ou"] = {
                "direction": "大球",
                "fair_p": round(float(fp), 4),
                "implied_p": round(float(ip), 4),
                "divergence": ov.get("divergence"),
            }
            out["available"] = True

    if not out["available"]:
        out["note"] = "需模型概率 + 去水平盘概率(或 OU over/under 赔率)方可计算 EV 代理; 当前数据不足。"
    return out


def _build_clv(analyze_out):
    """开盘→实时盘口漂移。仅在有 live 实时盘口时计算; 明确双重语义。"""
    out = {"available": False, "note": "", "ou_line_drift": None, "caveat": ""}
    md = (analyze_out or {}).get("model_data") or {}
    live = md.get("live") or {}
    ou = md.get("ou") or {}
    if live and live.get("ou") and live["ou"].get("line") is not None and ou.get("open_line") is not None:
        drift = round(float(live["ou"]["line"]) - float(ou["open_line"]), 3)
        out["available"] = True
        out["ou_line_drift"] = drift
        out["open_line"] = ou.get("open_line")
        out["live_line"] = live["ou"]["line"]
        out["caveat"] = ("漂移具双重语义: 既可能是庄家赔付管理(被动调线), 也可能是资金热度示警(主动调线); "
                         "方向不必然代表真实倾向, 须结合 evidence 闸门判定。")
    else:
        out["note"] = "滚球实时盘口缺失(纯赛前场景无 closing), 无法计算 CLV; 赛前仅提供开盘结构。"
    return out


def _build_ci(analyze_out, operator_card):
    """粗略置信带: 基于最高概率与次高概率的差距。明确非统计 CI。"""
    out = {"available": False, "note": "", "1x2": None, "operator": None}
    md = (analyze_out or {}).get("model_data") or {}
    x12 = md.get("1x2") or {}
    probs = {k: x12.get(k) for k in ("p_home", "p_draw", "p_away") if isinstance(x12.get(k), (int, float))}
    if probs:
        vals = sorted(probs.values(), reverse=True)
        best = vals[0]
        second = vals[1] if len(vals) >= 2 else 0.0
        half = (best - second) / 2.0
        out["1x2"] = {
            "p_home": [round(max(0, probs.get("p_home", 0) - half), 3), round(min(1, probs.get("p_home", 0) + half), 3)],
            "p_draw": [round(max(0, probs.get("p_draw", 0) - half), 3), round(min(1, probs.get("p_draw", 0) + half), 3)],
            "p_away": [round(max(0, probs.get("p_away", 0) - half), 3), round(min(1, probs.get("p_away", 0) + half), 3)],
            "band_half_width": round(half, 3),
        }
        out["available"] = True
    if operator_card and operator_card.get("confidence") is not None:
        out["operator"] = {"confidence": round(float(operator_card["confidence"]), 3)}
        out["available"] = True
    if not out["available"]:
        out["note"] = "无可用概率分布; 粗略置信带需模型概率或操盘手置信度。"
    else:
        out["note"] = ("粗略置信带 = 最高概率 ±½(最高−次高差幅); 非统计置信区间, "
                       "真实区间需 bootstrap 校准(当前样本/标签不足)。")
    return out
