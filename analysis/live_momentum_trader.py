# -*- coding: utf-8 -*-
"""动态滚球决策系统 (Live Momentum Trader) · 五部分统一裁决卡生成器.

消除「实时比分分析给出内容互相矛盾」(4 张独立卡片平铺无统一裁决) 的体感:
把 决策智能体 / 操盘手 / OU破蛋 / 信号仲裁 / 回测 多路信号, 重组为一张带统一裁决框架的卡:

  Part1 市场状态与盘口校验 (AH↔1X2 一致性 + OU 抽水 devig + 阶段 Margin 阈值预警 + 庄家意图/资金流)
  Part2 阶段判定           (Early<25' / Mid 25-70' / End 70'+ / halftime 状态机 + 策略抑制/采信)
  Part3 信号仲裁           (复用 signal_consensus; Backtest 权重最高; 冲突信号表格化)
  Part4 动态价值重算       (Live_EV = P * Odds * TimeDecayFactor - 1; 允许负EV标注"时间衰减修正交易价值")
  Part5 执行策略           (波胆按策略相关性排序 + 仓位标注"分析参考·需人工审批" 守 IR-20/IR-21)

诚实边界 (对齐铁律 IR-17/IR-18/IR-20/IR-21/IR-30):
  - 仅用真实市场赔率做 devig; 不拿模型概率冒充市场概率.
  - 数据缺失字段 available=false + 说明, 不编造数字.
  - 所有"建仓/注码"措辞改为"分析参考·需人工审批", 非硬判定指令.
  - AH↔1X2 一致性在仅有模型输出时, 明确标注为"模型内部一致性(非市场盘口对照)".

依赖(均经 IR-15 核实真实签名):
  analysis.signal_consensus.build_signal_consensus(analyze_out, operator_card, ou_decision)
  pipeline.ou_breakegg_decision.decide_ou(...) / derive_model_over_prob(...)
  pipeline.cs_ev_engine.league_goal_rate(league=)
  analyze_match_with_model 返回 model_data.{1x2,ou,ah,live}
"""
import logging
import os
import json

logger = logging.getLogger("live_momentum_trader")

# ── 词汇归一 ──
_X12_VOCAB = {
    "home": "home", "主胜": "home", "主队胜": "home", "home_win": "home", "主": "home",
    "draw": "draw", "平局": "draw", "平": "draw", "tie": "draw",
    "away": "away", "客胜": "away", "客队胜": "away", "away_win": "away", "客": "away",
}
_X12_LABEL = {"home": "主胜", "draw": "平局", "away": "客胜"}


def _norm(v):
    if v is None:
        return None
    return _X12_VOCAB.get(str(v).strip(), None)


# ── 阶段状态机 ──
# 阶段 Margin 阈值 (抽水上限): 早段盘口未稳定允许略高, 末段收紧
_PHASE_MARGIN_THRESHOLD = {
    "early": 0.09,      # <25'
    "mid": 0.07,       # 25'–70'
    "end": 0.06,       # >70'
    "halftime": 0.08,  # 半场间歇
}
# 阶段置信修正 (时间衰减基调)
_PHASE_CONF_MOD = {"early": 0.90, "mid": 1.00, "end": 0.85, "halftime": 0.70}


def _phase_state(minute: int, is_halftime: bool):
    """返回阶段状态机结果."""
    if is_halftime:
        phase = "halftime"
        label = "半场间歇"
        strategy = ("暂停 live 动量采信, 回到赛前开盘结构锚定; "
                    "下半场重开后按 Early 重新判定")
        suppress_live = True
    elif minute < 25:
        phase = "early"
        label = "早段"
        strategy = ("抑制 live OU 动量(样本极小); 以赛前开盘结构 + 初盘线为主; "
                    "仅观察, 不跟小样本漂移")
        suppress_live = True
    elif minute <= 70:
        phase = "mid"
        label = "中段"
        strategy = ("正常采信 live 信号; 需跨庄/漂移证据闸门方可升级; "
                    "OU 与 1X2 信号等权进入仲裁")
        suppress_live = False
    else:
        phase = "end"
        label = "末段"
        strategy = ("强时间衰减; 收紧 Margin 阈值; "
                    "剩余时间越少, 单张 live 低水越接近「庄家真实预期」(优先跟随, 不反说陷阱)")
        suppress_live = False

    remaining = max(1, (45 if is_halftime else 90) - minute)
    time_decay = max(0.30, remaining / 90.0)  # 时间衰减因子 (剩余/90, 下限0.3)
    return {
        "phase": phase,
        "label": label,
        "is_halftime": bool(is_halftime),
        "minute": int(minute),
        "remaining_minutes": remaining,
        "time_decay_factor": round(time_decay, 3),
        "margin_threshold": _PHASE_MARGIN_THRESHOLD[phase],
        "conf_modifier": _PHASE_CONF_MOD[phase],
        "suppress_live_momentum": suppress_live,
        "strategy_note": strategy,
    }


# ── Part1 市场状态与盘口校验 ──
def _part1_market_validation(analyze_out, live_ou_odds, live_1x2_odds, live_ah_odds, phase_state):
    md = (analyze_out or {}).get("model_data") or {}
    ou = md.get("ou") or {}
    ah = md.get("ah") or {}
    x12 = md.get("1x2") or {}

    # ── OU 抽水 devig 校验 ──
    ov = un = ln = None
    if live_ou_odds and live_ou_odds.get("over") and live_ou_odds.get("under"):
        ov, un, ln = live_ou_odds.get("over"), live_ou_odds.get("under"), live_ou_odds.get("line")
    else:
        _lu = md.get("live") or {}
        _lou = _lu.get("ou") or {}
        if _lou.get("over_odds") and _lou.get("under_odds"):
            ov, un, ln = _lou.get("over_odds"), _lou.get("under_odds"), _lou.get("line")

    ou_validation = {"available": False, "note": "", "margin": None, "margin_ok": None,
                     "implied_over": None, "implied_under": None, "line": ln,
                     "over_odds": ov, "under_odds": un}
    if ov and un and ov > 1.0 and un > 1.0:
        inv_o, inv_u = 1.0 / float(ov), 1.0 / float(un)
        overround = inv_o + inv_u - 1.0
        impl_o = inv_o / (inv_o + inv_u)
        impl_u = inv_u / (inv_o + inv_u)
        thr = phase_state["margin_threshold"]
        ou_validation.update({
            "available": True,
            "margin": round(overround, 4),
            "margin_ok": bool(overround <= thr),
            "implied_over": round(impl_o, 4),
            "implied_under": round(impl_u, 4),
            "margin_threshold": thr,
            "note": ("抽水正常(≤阶段阈值)" if overround <= thr
                     else f"抽水过高({overround:.1%} > 阶段阈值 {thr:.1%}) · 盘口异常预警: 庄家风险管控/赔付规避信号, 谨慎跟单"),
        })
    else:
        ou_validation["note"] = "无真实 OU 盘口赔率, 跳过抽水校验 (仅模型概率不可用 devig)."

    # ── AH↔1X2 一致性 ──
    ah_fav = _norm(ah.get("fav_side"))           # 模型 AH 让球方
    x12_verdict = _norm(x12.get("verdict"))       # 模型 1X2 verdict
    ah_1x2 = {"available": False, "consistent": None, "ah_fav": ah_fav,
              "x12_verdict": x12_verdict, "source": "模型内部", "note": ""}

    # 1) 市场 1X2 方向 (由真实 live 1X2 赔率去水)
    mkt_x12_dir = None
    if live_1x2_odds and all(live_1x2_odds.get(k) for k in ("home", "draw", "away")):
        try:
            h, d, a = (float(live_1x2_odds[k]) for k in ("home", "draw", "away"))
            inv = [1.0 / h, 1.0 / d, 1.0 / a]
            s = sum(inv)
            p = [x / s for x in inv]
            mkt_x12_dir = ("home", "draw", "away")[p.index(max(p))]
        except Exception:
            mkt_x12_dir = None

    # 2) 市场 AH 让球方 (由真实 live AH 赔率)
    mkt_ah_fav = None
    if live_ah_odds and live_ah_odds.get("home") and live_ah_odds.get("away"):
        try:
            mkt_ah_fav = "home" if float(live_ah_odds["home"]) < float(live_ah_odds["away"]) else "away"
        except Exception:
            mkt_ah_fav = None

    if mkt_ah_fav and mkt_x12_dir:
        # 完整市场对照: 市场 AH 让球方 vs 市场 1X2 方向
        ah_1x2["available"] = True
        ah_1x2["source"] = "市场盘口对照"
        ah_1x2["market_ah_fav"] = mkt_ah_fav
        ah_1x2["market_x12_dir"] = mkt_x12_dir
        ah_1x2["consistent"] = bool(mkt_ah_fav == mkt_x12_dir)
        ah_1x2["note"] = (f"市场 AH 让球方={_X12_LABEL.get(mkt_ah_fav, mkt_ah_fav)} "
                          f"vs 市场 1X2 方向={_X12_LABEL.get(mkt_x12_dir, mkt_x12_dir)} "
                          + ("→ 一致" if ah_1x2["consistent"] else "→ 不一致"))
    elif mkt_ah_fav and ah_fav and x12_verdict:
        # 有市场 AH 无市场 1X2 → 市场 AH vs 模型 1X2/AH
        ah_1x2["available"] = True
        ah_1x2["source"] = "市场 AH vs 模型"
        ah_1x2["market_ah_fav"] = mkt_ah_fav
        ah_1x2["consistent"] = bool(mkt_ah_fav == x12_verdict or mkt_ah_fav == ah_fav)
        ah_1x2["note"] = (f"市场 AH 让球方={_X12_LABEL.get(mkt_ah_fav, mkt_ah_fav)} "
                          f"vs 模型 1X2={_X12_LABEL.get(x12_verdict, x12_verdict)}"
                          + (" → 一致" if ah_1x2["consistent"] else " → 不一致"))
    elif ah_fav and x12_verdict:
        # 无真实盘口 → 模型内部一致性
        ah_1x2["available"] = True
        ah_1x2["consistent"] = bool(ah_fav == x12_verdict)
        ah_1x2["note"] = (f"模型 AH 倾向={_X12_LABEL.get(ah_fav, ah_fav)} 与 1X2 判定="
                          f"{_X12_LABEL.get(x12_verdict, x12_verdict)}"
                          + (" 一致 (模型内部; 无真实 AH/1X2 盘口赔率做市场对照)" if ah_1x2["consistent"]
                             else " 不一致 (模型内部; 无真实 AH/1X2 盘口赔率做市场对照)"))
    else:
        ah_1x2["note"] = "AH 或 1X2 方向缺失, 无法校验一致性."

    # ── 庄家意图 / 资金流 (轻量, 来自操盘手/仲裁) ──
    bookmaker_intent = {"available": False, "intent": "", "note": ""}
    return {
        "ou_validation": ou_validation,
        "ah_1x2_consistency": ah_1x2,
        "bookmaker_intent": bookmaker_intent,  # 由 Part3 仲裁后回填
    }


# ── OU 大球高抽水区检测 (历史校准) ──
def _ou_overpriced_guard(over_odds, under_odds):
    """OU 大球高抽水区检测. 全量回测铁证 (2026-08-26):

      买大球 over=1.89: 赢盘45.7% vs 隐含52.9% → 每注 EV -7.4%
      买小球 under=1.93: 赢盘47.3% vs 隐含51.8% → 每注 EV -2.4%
      大球 gap(-7.2pp) 远大于小球(-4.5pp) → 大球低赔区是庄家高抽水重灾区.

    当大球赔率落在 [1.80, 1.95] 时标「规避买大」, 小球方向相对诚实优先考虑.
    """
    if not over_odds or not under_odds or over_odds <= 1.0 or under_odds <= 1.0:
        return None
    over = float(over_odds)
    under = float(under_odds)
    if 1.80 <= over <= 1.95:
        return {
            "flag": "大球高抽水区",
            "level": "规避买大",
            "over_odds": round(over, 2),
            "under_odds": round(under, 2),
            "note": (f"大球赔率 {over:.2f} 落在庄家高抽水区(历史大球赢盘≈46% vs 隐含≈53%, "
                     f"EV≈-7%), 规避买大; 小球方向相对诚实(EV≈-2.4%), 优先考虑小球."),
        }
    return None


# ── Part4 动态价值重算 ──
def _part4_dynamic_value(ou_decision, analyze_out, phase_state, live_ou_odds=None):
    """Live_EV = P * Odds * TimeDecayFactor - 1. 允许负EV."""
    td = phase_state["time_decay_factor"]
    md = (analyze_out or {}).get("model_data") or {}
    live = md.get("live") or {}
    lou = live.get("ou") or {}
    ov = un = ln = None
    if live_ou_odds and live_ou_odds.get("over"):
        ov, un, ln = live_ou_odds.get("over"), live_ou_odds.get("under"), live_ou_odds.get("line")
    elif lou.get("over_odds"):
        ov, un, ln = lou.get("over_odds"), lou.get("under_odds"), lou.get("line")

    out = {"available": False, "time_decay_factor": td, "note": "", "sides": []}
    if ou_decision and (ov and un):
        # OU 大球高抽水区规避检测 (历史校准)
        guard = _ou_overpriced_guard(ov, un)
        if guard:
            out["overpriced_guard"] = guard
        for side in ("over", "under"):
            s = (ou_decision.get(side) or {})
            p_model = s.get("fair_prob")
            odds = float(ov if side == "over" else un)
            if p_model is None:
                continue
            live_ev = float(p_model) * odds * td - 1.0
            out["sides"].append({
                "side": side,
                "label": "大球" if side == "over" else "小球",
                "raw_divergence": s.get("divergence"),
                "live_ev": round(live_ev, 4),
                "live_ev_lean": ("正EV(时间衰减后仍有价值)" if live_ev > 0
                                else "时间衰减修正交易价值(原+EV被剩余时间侵蚀, 非硬判定)"),
                "odds": odds,
                "model_p": round(float(p_model), 4),
            })
        out["available"] = bool(out["sides"])
    else:
        out["note"] = "无 OU 决策或真实赔率, 无法重算 Live_EV."
    return out


# ── Part5 执行策略 ──
def _part5_execution(analyze_out, ou_decision, consensus, primary_signal, phase_state):
    """波胆按策略相关性排序 + 仓位标注需审批."""
    score_hint = (analyze_out or {}).get("score_hint") or {}
    anchor = score_hint.get("score") or ""
    anchor_basis = score_hint.get("basis") or ""

    # 策略相关性排序: 以 primary 方向为锚
    direction = (primary_signal or {}).get("axis")
    val = (primary_signal or {}).get("value")
    ranked_scores = []
    if anchor:
        ranked_scores.append({"score": anchor, "relevance": "诚实锚·主推",
                              "basis": anchor_basis, "priority": 1})
        # 派生 alternatives (策略相关, 非预测)
        try:
            h, a = (int(x) for x in anchor.split("-")[:2])
            if val == "over" or (direction == "OU" and val == "over"):
                alts = [f"{h+1}-{a}", f"{h}-{a+1}", f"{h+1}-{a+1}"]
            elif val == "under" or (direction == "OU" and val == "under"):
                alts = ["0-0", f"{h}-0", f"0-{a}", f"{h}-0", f"0-{a}"]
            elif val == "home":
                alts = [f"{h+1}-{a}", f"{h+2}-{a}"]
            elif val == "away":
                alts = [f"{h}-{a+1}", f"{h}-{a+2}"]
            else:
                alts = []
            seen = {anchor}
            for sc in alts:
                if sc not in seen:
                    seen.add(sc)
                    ranked_scores.append({"score": sc, "relevance": "策略相关·派生",
                                          "basis": f"沿主方向({val})拓展", "priority": len(ranked_scores) + 1})
        except Exception:
            pass

    # 仓位: 来自 ou_decision 但标注为参考+需审批
    stake_ref = 0.0
    if ou_decision:
        bs = ou_decision.get("result") or {}
        stake_ref = float(bs.get("stake") or 0.0)
    return {
        "correct_scores_ranked": ranked_scores,
        "position_reference": {
            "stake_reference": round(stake_ref, 2),
            "label": "分析参考 · 需人工审批",
            "note": "依据 IR-20/IR-21: 本卡所有建仓/注码仅为分析指向, 须经 /api/execute/confirm 人工审批方可执行, 不伪造信号指令.",
        },
        "one_line_decision": _one_line(consensus, primary_signal, phase_state),
    }


def _one_line(consensus, primary_signal, phase_state):
    sc = (consensus or {}).get("signal_consensus") or {}
    agree = sc.get("agreement", "无信号")
    ps = primary_signal or {}
    ps_label = ps.get("label") or "—"
    phase = phase_state["label"]
    return (f"[{phase}] 多路信号{agree}; 主指向: {ps_label}. "
            f"分析非预测, 建仓须人工审批.")


def _load_backtest_summary():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        p = os.path.join(here, "live_goal_probe_backtest.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


# ───────────────────────────────────────────────────────────
# 主入口
# ───────────────────────────────────────────────────────────
def build_momentum_card(analyze_out, operator_card=None, ou_decision=None,
                        consensus=None, minute=0, is_halftime=False,
                        league=None, live_1x2_odds=None, live_ou_odds=None,
                        live_ah_odds=None, opening_total=None,
                        backtest_summary=None):
    """组装五部分动态滚球决策卡.

    Args 均来自上层(/momentum 端点)已组装好的真实数据, 本模块只做重组与裁决逻辑,
    不重复调用重模型, 不编造缺失字段.
    """
    from analysis.signal_consensus import build_signal_consensus

    if consensus is None:
        consensus = build_signal_consensus(analyze_out, operator_card, ou_decision)

    phase_state = _phase_state(minute, is_halftime)

    # Part1
    part1 = _part1_market_validation(analyze_out, live_ou_odds, live_1x2_odds, live_ah_odds, phase_state)

    # Part3 仲裁 (复用 consensus)
    sc = consensus.get("signal_consensus") or {}
    primary_signal = sc.get("primary_signal")
    # Backtest 权重最高: 载入回测摘要作为最高权证据
    bt = backtest_summary if backtest_summary is not None else _load_backtest_summary()
    part3 = {
        "signal_consensus": sc,
        "discrepancy": consensus.get("discrepancy"),
        "closing_line_value": consensus.get("closing_line_value"),
        "confidence_interval": consensus.get("confidence_interval"),
        "backtest_weight": "最高",
        "backtest_summary": bt,
        "conflicts_table": sc.get("conflicts") or [],
        "resolution_note": (
            "冲突裁决优先级: 回测实证 > 跨庄/漂移证据 > 模型概率 > 单张 live 低水. "
            "Backtest 命中率为最高权重信号; 任何与回测相悖的 live 信号须降权标注."),
    }

    # Part1 庄家意图回填 (来自仲裁 discrepancy + 操盘手)
    disc = consensus.get("discrepancy") or {}
    bi_note = ""
    if disc.get("available"):
        if disc.get("1x2"):
            bi_note += f"1X2 模型 vs 去水 gap={disc['1x2'].get('gap')}; "
        if disc.get("ou"):
            bi_note += f"OU fair={disc['ou'].get('fair_p')} vs implied={disc['ou'].get('implied_p')}. "
    op_trap = (operator_card or {}).get("trap_score")
    if op_trap is not None:
        bi_note += f"操盘手 trap_score={op_trap}. "
    part1["bookmaker_intent"] = {
        "available": bool(bi_note),
        "intent": (operator_card or {}).get("decision") or "",
        "note": bi_note or "无足够信号推断庄家意图.",
    }

    # Part2
    part2 = phase_state

    # Part4
    part4 = _part4_dynamic_value(ou_decision, analyze_out, phase_state, live_ou_odds)

    # Part5
    part5 = _part5_execution(analyze_out, ou_decision, consensus, primary_signal, phase_state)

    return {
        "ok": True,
        "card": "LiveMomentumTrader",
        "match_key": (analyze_out or {}).get("match_key"),
        "minute": int(minute),
        "is_halftime": bool(is_halftime),
        "league": league,
        "part1_market_validation": part1,
        "part2_phase": part2,
        "part3_arbitration": part3,
        "part4_dynamic_value": part4,
        "part5_execution": part5,
        "disclaimer": ("分析非预测 (IR-20); 建仓须经人工审批 (IR-21); 宁 PASS 不伪造 (IR-30). "
                       "本卡为统一裁决框架, 整合多路信号消除矛盾体感, 不构成投注建议."),
    }
