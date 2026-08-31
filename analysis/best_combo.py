"""best_combo — 4 盘口诚实综合分析与候选信号 (2026-08-31, IR-30)
================================================================
给定一场比赛的盘口赔率, 输出 胜平负/大小球/让球/波胆 四条件的综合分析与候选信号。
设计原则(严守 IR-20/IR-21/IR-30):
- 所有"edge"标注都来自 scripts/evaluate_best_combination_20260831.py 的收盘现实价压测证据。
- OU 低线(2.0-2.75) = 唯一通过收盘价+CI 审视的候选(前向监控中, 未部署/未真实下注)。
- AH = 真实但薄弱的"狗覆盖偏差", 标注为弱候选(待按线校准+前向)。
- 1X2 = 方向分析(无验证ROI edge)。
- 波胆 = 泊松 top 比分概率分布, 明确"非单点预测"(IR-03: CS=庄家诱导器)。

前端直接传入开盘赔率即可实时用; 若传 mk 则额外给 CS 信任卡(结构/庄家/历史实证)。
"""
from __future__ import annotations
import os, sys, json, sqlite3, math
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.fl_predictor import predict_from_odds
from pipeline.poisson_gbm import available as gbm_ok, predict_lambdas

OU_LOWLINES = (2.0, 2.75)   # 唯一通过诚实压测的 OU 子集
EDGE_MIN = 0.02


def _devig3(oh, od, oa):
    inv = 1/oh + 1/od + 1/oa
    return (1/oh)/inv, (1/od)/inv, (1/oa)/inv

def _devig2(o0, o1):
    inv = 1/o0 + 1/o1
    return (1/o0)/inv, (1/o1)/inv

def _poisson_pmf(k, lam):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    if k < 0:
        return 0.0
    return math.exp(k*math.log(lam) - lam - sum(math.log(i) for i in range(1, k+1)))

def _poisson_cover(lh, la, line):
    ph = pa = pp = 0.0
    for i in range(0, 13):
        for j in range(0, 13):
            pr = _poisson_pmf(i, lh) * _poisson_pmf(j, la)
            d = i - j
            if d > -line: ph += pr
            elif d < -line: pa += pr
            else: pp += pr
    return ph, pa, pp

def _cs_top(lh, la, topn=5):
    grid = {}
    for i in range(0, 9):
        for j in range(0, 9):
            grid[(i, j)] = _poisson_pmf(i, lh) * _poisson_pmf(j, la)
    ranked = sorted(grid.items(), key=lambda kv: -kv[1])[:topn]
    return [{"score": f"{i}:{j}", "prob": round(p, 4)} for (i, j), p in ranked]


def analyze_best_combo(home, away, oh, od, oa, league,
                       line=None, ov=None, un=None,
                       ah_line=None, ah_h=None, ah_a=None,
                       ko=None, mk=None, con=None):
    """返回 4 盘口综合分析与候选信号(dict)。

    赔率: oh/od/oa = 1X2; line/ov/un = OU 主盘; ah_line/ah_h/ah_a = AH 主盘。
    """
    out = {"home": home, "away": away, "league": league, "honesty": "分析非预测; 候选信号未部署未真实下注(IR-20/IR-21/IR-30)"}
    fl = predict_from_odds(h=oh, d=od, a=oa, ou_line=line, ou_over=ov, ou_under=un,
                           ah_line=ah_line, ah_home=ah_h, ah_away=ah_a,
                           league=league, kickoff=ko) if all(x is not None for x in (line, ov, un)) else None
    if fl is None:
        # 缺 OU/AH 时仍给 1X2 + CS
        fl = predict_from_odds(h=oh, d=od, a=oa, league=league, kickoff=ko)

    # ---- 1X2 ----
    x2 = fl.get("1x2") if fl else None
    if x2:
        idx = int(__import__("numpy").argmax(x2)) if hasattr(x2, "argmax") else max(range(3), key=lambda i: x2[i])
        out["x2"] = {
            "probs": [round(float(p), 4) for p in x2],
            "direction": ["主", "平", "客"][idx],
            "label": "方向分析(无验证ROI edge)",
            "verdict": "分析信号",
        }

    # ---- OU (低线窄策略 = 唯一候选) ----
    if fl and fl.get("ou") and line is not None and ov and un:
        p_over = float(fl["ou"][0]) if fl["ou"] else 0.5  # fl_model_ou 已下线(2026-08-31) → 中性0.5
        imp_over = _devig2(ov, un)[0]
        edge = p_over - imp_over
        in_low = OU_LOWLINES[0] <= line <= OU_LOWLINES[1]
        if in_low and abs(edge) >= EDGE_MIN:
            out["ou"] = {
                "line": line, "p_over": round(p_over, 4), "imp_over": round(imp_over, 4),
                "edge": round(edge, 4), "side": "大" if edge > 0 else "小",
                "label": "候选edge(低线窄策略, 前向监控中, 未部署)",
                "verdict": "候选信号",
            }
        else:
            out["ou"] = {
                "line": line, "p_over": round(p_over, 4), "imp_over": round(imp_over, 4),
                "edge": round(edge, 4),
                "label": ("线不在低线区间(2.0-2.75), 通用overlay收盘CI跨零→无信号"
                          if not in_low else "edge不足阈值→无信号"),
                "verdict": "无信号",
            }

    # ---- AH (狗覆盖偏差 = 弱候选) ----
    if ah_line is not None and ah_h and ah_a and gbm_ok():
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
        if lam:
            ph, pa, pp = _poisson_cover(lam[0], lam[1], ah_line)
            imp_h, imp_a = _devig2(ah_h, ah_a)
            edge = ph - imp_h
            fav_cover_note = "热门方实际覆盖率仅44.72%(本数据集), 狗>50% → 真实但薄弱偏差"
            out["ah"] = {
                "line": ah_line, "poisson_home_cover": round(ph, 4),
                "imp_home_cover": round(imp_h, 4), "edge": round(edge, 4),
                "push_prob": round(pp, 4),
                "label": "弱候选(狗覆盖偏差; 待按线校准+前向验证)",
                "verdict": "弱候选",
                "note": fav_cover_note,
            }

    # ---- CS (波胆 = 概率分布, 非单点预测) ----
    lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league) if gbm_ok() else None
    if lam:
        out["cs"] = {
            "top_scorelines": _cs_top(lam[0], lam[1], 5),
            "label": "泊松top5比分概率分布(非单点预测; IR-03: CS=庄家诱导器)",
            "verdict": "概率分布",
        }
    # 若传了 mk + con, 附 CS 信任卡(结构/庄家/历史实证)
    if mk and con is not None:
        try:
            from pipeline.cs_trust_model import build_trust_card
            tc = build_trust_card(con, mk)
            if tc:
                out["cs"]["trust_card"] = tc
        except Exception:
            pass

    return out


if __name__ == "__main__":
    # 快速自测(用一组示例赔率)
    demo = analyze_best_combo("主队", "客队", 2.1, 3.4, 3.2, "测试联赛",
                              line=2.5, ov=1.9, un=1.9, ah_line=-0.5, ah_h=1.9, ah_a=1.9)
    print(json.dumps(demo, ensure_ascii=False, indent=2))
