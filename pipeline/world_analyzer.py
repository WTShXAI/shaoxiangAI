# -*- coding: utf-8 -*-
"""
pipeline.world_analyzer — 哨响AI 世界级足球分析器 v1 (2026-08-31)
================================================================

定位: 把"市场锚 + 最强模型矩阵 + 三件套 + 联赛背景 + 诚实边界"整合为一个
可解释、可回测、可上生产前端的综合分析器。分析非预测(IR-20), 报 edge 必须
三件套: win_rate / implied / edge_pp(+EV 口径铁律 08-31)。

组件矩阵 (全部惰性加载, 任一缺失 → None, 零回归):
  ┌──────────────────┬──────────────────────────────────────────────┐
  │ 组件              │ 来源                                          │
  ├──────────────────┼──────────────────────────────────────────────┤
  │ 市场锚(去水隐含)  │ 输入赔率 devig —— 单庄隐含 AUC≈0.73, 金标准      │
  │ independent_1x2  │ pipeline.predictors.saved_models (最强单模型)  │
  │ fl_1x2 / fl_ah   │ data/fl_model_{1x2,ah}.joblib (结构树模型)     │
  │ fused_1x2        │ models/fused_1x2_20260831.joblib (LR 融合)     │
  │ poisson_ou       │ pipeline.poisson_gbm (OIP+GBM λ)              │
  │ 联赛背景          │ data/football_data.db matches 联赛统计          │
  │ 漂移              │ 可选开盘价 → 开盘→当前漂移(三段框架初盘→临场)     │
  └──────────────────┴──────────────────────────────────────────────┘

铁律:
  - 不给模型喂伪造特征: direction_model 需要 odds_features 完整特征
    (open/close/drift/sigma_trap), 缺失时跳过并标注, 绝不补 0 硬推。
  - OU 走纯泊松: fl_model_ou 已下线(AUC 0.523 < baseline, 2026-08-31 删除),
    fused_ou 特征缺失 → OU 概率统一由 poisson p_over + 市场锚承担。
  - 假 0-0 过滤: 回测/统计凡用到 matches.score_* 必须先过滤
    (见 MEMORY 数据质量地雷)。

CLI:
  python -m pipeline.world_analyzer --home 皇家马德里 --away 巴塞罗那 \
      --league 西甲 --h 2.1 --d 3.4 --a 3.6 \
      [--ou-line 2.5 --ou-over 1.9 --ou-under 1.9] \
      [--ah-line 0.5 --ah-home 1.9 --ah-away 1.9] \
      [--op-h 2.05 --op-d 3.5 --op-a 3.8] [--kickoff 2026-09-01 03:00]

回测:
  python -m pipeline.world_analyzer --backtest --league 西甲 --n 800 [--since 2018]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "data", "football_data.db")
VERSION = "1.1.0"
GENERATED_AT = time.strftime("%Y-%m-%d %H:%M:%S")

_lock = threading.Lock()
_caches: Dict[str, object] = {}


# ─────────────────────────────────────────────────────────────────
#  基础工具
# ─────────────────────────────────────────────────────────────────
def devig(ps: List[float]) -> List[float]:
    """去水: p_i / Σp_i"""
    s = sum(ps)
    if s <= 0:
        return [0.0] * len(ps)
    return [p / s for p in ps]


def overround(odds: List[float]) -> Optional[float]:
    if not odds or any(not o or o <= 0 for o in odds):
        return None
    return sum(1.0 / o for o in odds) - 1.0


def implied(odds: float) -> Optional[float]:
    if not odds or odds <= 0:
        return None
    return 1.0 / odds


def _load_joblib(path: str):
    with _lock:
        if path in _caches:
            return _caches[path]
        import joblib
        if not os.path.exists(path):
            _caches[path] = None
            return None
        try:
            m = joblib.load(path)
            _caches[path] = m
            return m
        except Exception:
            _caches[path] = None
            return None


# ─────────────────────────────────────────────────────────────────
#  市场锚
# ─────────────────────────────────────────────────────────────────
def market_anchor(h=None, d=None, a=None,
                  ou_line=None, ou_over=None, ou_under=None,
                  ah_line=None, ah_home=None, ah_away=None) -> dict:
    out: Dict = {"1x2_devig": None, "overround": None, "ou_over_devig": None,
                 "ah_home_devig": None, "ou_line": ou_line, "ah_line": ah_line}
    if h and d and a:
        out["1x2_devig"] = devig([1.0 / h, 1.0 / d, 1.0 / a])
        out["overround"] = overround([h, d, a])
    if ou_line is not None and ou_over and ou_under:
        p = devig([1.0 / ou_over, 1.0 / ou_under])
        out["ou_over_devig"] = p[0]
    if ah_line is not None and ah_home and ah_away:
        p = devig([1.0 / ah_home, 1.0 / ah_away])
        out["ah_home_devig"] = p[0]
    return out


# ─────────────────────────────────────────────────────────────────
#  模型矩阵 (惰性)
# ─────────────────────────────────────────────────────────────────
def _independent_1x2(home: str, away: str, h, d, a):
    """最强单模型 (independent_model.joblib) —— 查不到 indep_features 返回 None"""
    try:
        from pipeline.independent_predictor import predict_1x2
        return predict_1x2(home, away, float(h), float(d), float(a))
    except Exception:
        return None


def _fl_signals(h=None, d=None, a=None, ou_line=None, ou_over=None, ou_under=None,
                ah_line=None, ah_home=None, ah_away=None, league=None, kickoff=None):
    """结构树模型: fl_1x2 + fl_ah (fl_ou 已下线)"""
    try:
        from pipeline.fl_predictor import predict_from_odds
        r = predict_from_odds(h=h, d=d, a=a, ou_line=ou_line, ou_over=ou_over,
                              ou_under=ou_under, ah_line=ah_line, ah_home=ah_home,
                              ah_away=ah_away, league=league, kickoff=kickoff)
        return r
    except Exception:
        return {"1x2": None, "ou": None, "ah": None}


def _fused_1x2(fl_1x2, fl_ah):
    """LR 融合模型 (fused_1x2_20260831): 输入 [fl_H,fl_D,fl_A,fl_AH_H,fl_AH_A]"""
    if not fl_1x2 or not fl_ah:
        return None
    m = _load_joblib(os.path.join(_ROOT, "models", "fused_1x2_20260831.joblib"))
    if not m:
        return None
    try:
        import numpy as np
        X = np.array([[fl_1x2[0], fl_1x2[1], fl_1x2[2], fl_ah[0], fl_ah[1]]], dtype=float)
        return [float(x) for x in m["meta"].predict_proba(X)[0]]
    except Exception:
        return None


def _poisson_ou(h=None, d=None, a=None, ou_line=None, league=None):
    """泊松 OIP+GBM → P(over)"""
    if not (h and d and a) or ou_line is None:
        return None
    try:
        from pipeline.poisson_gbm import available, predict_lambdas, p_over
        if not available():
            return None
        lam = predict_lambdas(float(h), float(d), float(a),
                              ch=float(h), cd=float(d), ca=float(a), league=league)
        if not lam:
            return None
        return float(p_over(lam[0], lam[1], float(ou_line)))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────
#  联赛背景 (football_data.db)
# ─────────────────────────────────────────────────────────────────
def league_context(league: Optional[str]) -> Optional[dict]:
    if not league:
        return None
    try:
        con = sqlite3.connect(DB, timeout=30)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT league_name, COUNT(*) n, "
            "AVG(CASE WHEN final_result='H' THEN 1.0 ELSE 0 END) hr, "
            "AVG(CASE WHEN final_result='D' THEN 1.0 ELSE 0 END) dr, "
            "AVG(CASE WHEN final_result='A' THEN 1.0 ELSE 0 END) ar, "
            "AVG(home_score+away_score) avg_goals "
            "FROM matches WHERE status='finished' AND league_name LIKE ? "
            "GROUP BY league_name ORDER BY COUNT(*) DESC LIMIT 1",
            (f"%{league}%",)).fetchone()
        con.close()
        if rows and rows["n"] and rows["n"] > 0:
            return {"league": rows["league_name"], "n": int(rows["n"]),
                    "home_rate": round(float(rows["hr"]), 4),
                    "draw_rate": round(float(rows["dr"]), 4),
                    "away_rate": round(float(rows["ar"]), 4),
                    "avg_goals": round(float(rows["avg_goals"]), 3)}
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────
#  漂移 (开盘 → 当前)
# ─────────────────────────────────────────────────────────────────
def drift_signal(op_h=None, op_d=None, op_a=None, h=None, d=None, a=None) -> Optional[dict]:
    if not (op_h and op_d and op_a and h and d and a):
        return None
    op = devig([1.0 / op_h, 1.0 / op_d, 1.0 / op_a])
    cur = devig([1.0 / h, 1.0 / d, 1.0 / a])
    drift_pp = [(c - o) * 100.0 for c, o in zip(cur, op)]
    return {"opening": op, "current": cur,
            "drift_pp_home": round(drift_pp[0], 2),
            "drift_pp_draw": round(drift_pp[1], 2),
            "drift_pp_away": round(drift_pp[2], 2),
            "strongest_drift": max(drift_pp, key=abs)}


# ─────────────────────────────────────────────────────────────────
#  一致性 / edge 三件套
# ─────────────────────────────────────────────────────────────────
def _consensus(model_probs: List[List[float]]) -> Optional[List[float]]:
    valid = [p for p in model_probs if p]
    if not valid:
        return None
    n = len(valid[0])
    return [sum(p[i] for p in valid) / len(valid) for i in range(n)]


def edge_triple(side: str, model_p: float, odds: float) -> dict:
    """+EV 唯一判据: 实际胜率(model_p) > 隐含概率(1/odds) + 抽水.
    返回三件套: win_rate / implied / edge_pp."""
    imp = implied(odds)
    edge_pp = (model_p - imp) * 100.0 if imp else None
    return {"side": side, "win_rate": round(model_p, 4),
            "implied": round(imp, 4) if imp else None,
            "edge_pp": round(edge_pp, 2) if edge_pp is not None else None}


# ─────────────────────────────────────────────────────────────────
#  CS 波胆模型 (2026-08-31, 达标闸门: top1 须超越市场基线才激活)
# ─────────────────────────────────────────────────────────────────
CS_CLASSES = [f"{h}:{a}" for h in range(5) for a in range(5)] + ["其他"]  # 26 类 (0:0..4:4+其他)
_CS_LR_PATH = os.path.join(_ROOT, "models", "cs_odds_lr.joblib")
_CS_REPORT_PATH = os.path.join(_ROOT, "models", "cs_odds_report.json")
# 训练集缺失值中位数 (对齐 scripts/train_cs_odds_model.py 填充, 无特征时兜底)
_CS_MEDIAN = {"ou_line": 1.5, "ou_over_devig": 0.5212, "ah_line": -0.25,
              "ah_home_devig": 0.4897, "lg_avg_goals": 2.8333,
              "lg_home_win": 0.3793, "lg_draw": 0.25}


def _cs_qualified() -> Tuple[bool, str]:
    """达标闸门: LR top1 须 > 市场 CS 基线 top1 (超越市场才接入, 用户拍板口径 08-31).
    读取 models/cs_odds_report.json (时间外测试集指标)."""
    try:
        with open(_CS_REPORT_PATH, encoding="utf-8") as f:
            rep = json.load(f)
        top1_mkt = next(m["top1"] for m in rep["results"] if m["model"] == "market_cs")
        top1_lr = next(m["top1"] for m in rep["results"] if m["model"] == "logistic_regression")
        if top1_lr > top1_mkt:
            return True, f"达标(top1 {top1_lr*100:.1f}% > 市场 {top1_mkt*100:.1f}%)"
        return False, f"未达标(top1 {top1_lr*100:.1f}% < 市场 {top1_mkt*100:.1f}%), 未参与判定"
    except Exception as e:
        return False, f"门槛检查失败({e})"


def _cs_model(cs_odds: Optional[Dict[str, float]] = None,
              h=None, d=None, a=None,
              ou_line=None, ou_over=None, ou_under=None,
              ah_line=None, ah_home=None, ah_away=None,
              league: Optional[str] = None) -> Optional[dict]:
    """CS 波胆模型推理 (26 类). 返回:
      {"qualified": bool, "reason": str, "top5": [[比分,概率]×5]|None, "three_way": [h,d,a]|None}
    None = 内部错误(赔率非法)."""
    q, reason = _cs_qualified()
    if not q:
        return {"qualified": False, "reason": reason, "top5": None, "three_way": None}
    if not cs_odds or len([s for s in CS_CLASSES if s in cs_odds]) < 26:
        return {"qualified": True, "reason": "无 CS 赔率输入(26选), 无法推理", "top5": None, "three_way": None}
    try:
        inv_sum = sum(1.0 / o for o in cs_odds.values() if o and 0 < o <= 1000)
        if not (0.9 <= inv_sum <= 1.6):
            return {"qualified": True, "reason": f"CS 抽水异常({inv_sum:.3f}), 跳过", "top5": None, "three_way": None}
        dv = {s: (1.0 / cs_odds[s]) / inv_sum for s in CS_CLASSES}
        # 特征向量 (对齐 train_cs_odds_model.FEATURE_COLS 38 列)
        h2h_h, h2h_d, h2h_a = (float(h), float(d), float(a)) if (h and d and a) else (1/3, 1/3, 1/3)
        ou_over_dv = None
        if ou_line is not None and ou_over and ou_under:
            p = devig([1.0 / ou_over, 1.0 / ou_under])
            ou_over_dv = p[0]
        ah_home_dv = None
        if ah_line is not None and ah_home and ah_away:
            p = devig([1.0 / ah_home, 1.0 / ah_away])
            ah_home_dv = p[0]
        lg = league_context(league)
        feats = [dv[s] for s in CS_CLASSES]                       # 26
        feats.append(min(dv.values()))                            # cs_cheapest_p
        feats.append(inv_sum)                                     # cs_overround
        feats += [h2h_h, h2h_d, h2h_a]                            # 3
        feats += [float(ou_line) if ou_line is not None else _CS_MEDIAN["ou_line"],
                  ou_over_dv if ou_over_dv is not None else _CS_MEDIAN["ou_over_devig"]]
        feats += [float(ah_line) if ah_line is not None else _CS_MEDIAN["ah_line"],
                  ah_home_dv if ah_home_dv is not None else _CS_MEDIAN["ah_home_devig"]]
        feats += [lg["avg_goals"] if lg else _CS_MEDIAN["lg_avg_goals"],
                  lg["home_rate"] if lg else _CS_MEDIAN["lg_home_win"],
                  lg["draw_rate"] if lg else _CS_MEDIAN["lg_draw"]]
        m = _load_joblib(_CS_LR_PATH)
        if not m:
            return {"qualified": True, "reason": "模型文件缺失", "top5": None, "three_way": None}
        import numpy as np
        p26 = m["model"].predict_proba(np.asarray([feats], dtype=np.float64))[0]
        order = np.argsort(-p26)
        top5 = [[CS_CLASSES[i], round(float(p26[i]), 4)] for i in order[:5]]
        # 三方向聚合 (其他档等分, 与训练脚本 three_way 一致)
        idx = np.arange(25).reshape(5, 5)
        hw, aw = idx[np.tril_indices(5, -1)], idx[np.triu_indices(5, 1)]
        dr = np.diag(idx)
        other = float(p26[25])
        tw = [float(p26[hw].sum()) + other / 3.0,
              float(p26[dr].sum()) + other / 3.0,
              float(p26[aw].sum()) + other / 3.0]
        return {"qualified": True, "reason": reason, "top5": top5,
                "three_way": [round(x, 4) for x in tw]}
    except Exception as e:
        return {"qualified": True, "reason": f"推理失败({e})", "top5": None, "three_way": None}


# ─────────────────────────────────────────────────────────────────
#  主分析
# ─────────────────────────────────────────────────────────────────
def analyze_match(home: str, away: str, league: Optional[str] = None,
                  h=None, d=None, a=None,
                  ou_line=None, ou_over=None, ou_under=None,
                  ah_line=None, ah_home=None, ah_away=None,
                  op_h=None, op_d=None, op_a=None,
                  kickoff: Optional[str] = None,
                  cs_odds: Optional[Dict[str, float]] = None) -> dict:
    t0 = time.time()
    flags: List[str] = []

    # ── 市场锚 ──
    mkt = market_anchor(h, d, a, ou_line, ou_over, ou_under, ah_line, ah_home, ah_away)
    if not mkt["1x2_devig"]:
        flags.append("无 1X2 赔率 → 市场锚不可用, 模型无对照")
    if mkt["overround"] and mkt["overround"] > 0.15:
        flags.append(f"抽水过高({mkt['overround']*100:.1f}%>15%), 价值层打折(IR-18)")

    # ── 模型矩阵 ──
    indep = _independent_1x2(home, away, h, d, a) if (h and d and a) else None
    if indep is None:
        flags.append("independent 模型无该对阵特征(indep_features), 跳过(最强单模型缺席)")

    fl = _fl_signals(h=h, d=d, a=a, ou_line=ou_line, ou_over=ou_over, ou_under=ou_under,
                     ah_line=ah_line, ah_home=ah_home, ah_away=ah_away,
                     league=league, kickoff=kickoff)
    fl_1x2, fl_ah = fl.get("1x2"), fl.get("ah")
    if fl_1x2 is None:
        flags.append("fl_1x2 不可用(结构特征缺失)")
    if fl_ah is None:
        flags.append("fl_ah 不可用 → 让球方向缺失")

    fused = _fused_1x2(fl_1x2, fl_ah)
    if fused is None and fl_1x2 and fl_ah:
        flags.append("fused_1x2 融合失败(回退 fl 单模型)")

    po_over = _poisson_ou(h, d, a, ou_line, league)

    # ── 一致性 ──
    model_pool = [p for p in (indep, fl_1x2, fused) if p]
    avg_1x2 = _consensus(model_pool)
    lean = None
    vs_market = None
    if avg_1x2 and mkt["1x2_devig"]:
        idx = max(range(3), key=lambda i: avg_1x2[i])
        lean = ["主胜", "平局", "客胜"][idx]
        vs_market = [round((m - mm) * 100.0, 2) for m, mm in zip(avg_1x2, mkt["1x2_devig"])]

    # ── edge 三件套 ──
    edge = None
    if avg_1x2 and mkt["1x2_devig"] and (h and d and a):
        idx = max(range(3), key=lambda i: avg_1x2[i])
        odds_side = [h, d, a][idx]
        edge = edge_triple(["H", "D", "A"][idx], avg_1x2[idx], odds_side)

    # ── OU 一致性 ──
    ou_verdict = None
    if ou_line is not None and mkt["ou_over_devig"] is not None:
        model_ou = po_over if po_over is not None else mkt["ou_over_devig"]
        ou_verdict = {
            "model_p_over": round(model_ou, 4),
            "market_p_over": round(mkt["ou_over_devig"], 4),
            "edge_pp": round((model_ou - mkt["ou_over_devig"]) * 100.0, 2),
            "lean": "大" if model_ou > mkt["ou_over_devig"] + 0.02 else
                    ("小" if model_ou < mkt["ou_over_devig"] - 0.02 else "中性"),
        }

    # ── CS 波胆模型 (达标闸门: 未超越市场基线 → 不参与判定, 诚实标注) ──
    cs_odds_result = _cs_model(cs_odds, h=h, d=d, a=a,
                               ou_line=ou_line, ou_over=ou_over, ou_under=ou_under,
                               ah_line=ah_line, ah_home=ah_home, ah_away=ah_away,
                               league=league)
    if cs_odds_result is not None and not cs_odds_result.get("qualified") and cs_odds:
        flags.append(f"CS 波胆模型 {cs_odds_result.get('reason', '未达标')}")

    # ── 联赛背景 / 漂移 ──
    lg = league_context(league)
    drift = drift_signal(op_h, op_d, op_a, h, d, a)
    if not drift and not (op_h and op_d and op_a):
        flags.append("无开盘价 → 无漂移信号(三段框架初盘锚缺失)")

    return {
        "version": VERSION, "generated_at": GENERATED_AT,
        "match": {"home": home, "away": away, "league": league, "kickoff": kickoff},
        "market": mkt,
        "models": {
            "independent_1x2": [round(x, 4) for x in indep] if indep else None,
            "fl_1x2": [round(x, 4) for x in fl_1x2] if fl_1x2 else None,
            "fl_ah": [round(x, 4) for x in fl_ah] if fl_ah else None,
            "fused_1x2": [round(x, 4) for x in fused] if fused else None,
            "poisson_over": round(po_over, 4) if po_over is not None else None,
            "cs_odds": cs_odds_result,
        },
        "consensus": {
            "model_avg_1x2": [round(x, 4) for x in avg_1x2] if avg_1x2 else None,
            "lean": lean,
            "vs_market_pp": vs_market,
            "n_models": len(model_pool),
        },
        "edge_1x2": edge,
        "ou": ou_verdict,
        "drift": drift,
        "league_context": lg,
        "honest_flags": flags,
        "runtime_ms": int((time.time() - t0) * 1000),
    }


def _fmt_pcts(ps: Optional[List[float]]) -> str:
    if not ps:
        return "N/A"
    return " / ".join(f"{p*100:.1f}%" for p in ps)


def print_report(r: dict) -> None:
    m = r["match"]
    line = "=" * 72
    print(line)
    print(f"世界级足球分析器 v{r['version']}  @ {r['generated_at']}")
    print(f"{m['home']} vs {m['away']}" + (f"  [{m['league']}]" if m["league"] else "") +
          (f"  {m['kickoff']}" if m["kickoff"] else ""))
    print(line)
    mk = r["market"]
    print("■ 市场锚 (去水隐含概率)")
    if mk["1x2_devig"]:
        print(f"  1X2: 主{_fmt_pcts([mk['1x2_devig'][0]])} 平{_fmt_pcts([mk['1x2_devig'][1]])} "
              f"客{_fmt_pcts([mk['1x2_devig'][2]])}  抽水{(mk['overround'] or 0)*100:.1f}%")
    if mk["ou_line"] is not None and mk["ou_over_devig"] is not None:
        print(f"  OU: 线{mk['ou_line']} 大{mk['ou_over_devig']*100:.1f}% 小{(1-mk['ou_over_devig'])*100:.1f}%")
    if mk["ah_line"] is not None and mk["ah_home_devig"] is not None:
        print(f"  AH: 线{mk['ah_line']} 主{mk['ah_home_devig']*100:.1f}% 客{(1-mk['ah_home_devig'])*100:.1f}%")
    md = r["models"]
    print("\n■ 模型矩阵")
    print(f"  independent_1x2: {_fmt_pcts(md['independent_1x2'])}" if md["independent_1x2"] else "  independent_1x2: 不可用(无特征)")
    print(f"  fl_1x2          : {_fmt_pcts(md['fl_1x2'])}" if md["fl_1x2"] else "  fl_1x2          : 不可用")
    print(f"  fused_1x2       : {_fmt_pcts(md['fused_1x2'])}" if md["fused_1x2"] else "  fused_1x2       : 不可用")
    if md["fl_ah"]:
        print(f"  fl_ah(让球)     : 主{md['fl_ah'][0]*100:.1f}% 客{md['fl_ah'][1]*100:.1f}%")
    if md["poisson_over"] is not None:
        print(f"  poisson OU P(大): {md['poisson_over']*100:.1f}%")
    csod = md.get("cs_odds")
    if csod is not None:
        if csod.get("top5"):
            top5s = " / ".join(f"{s}({p*100:.1f}%)" for s, p in csod["top5"])
            print(f"  cs_odds(波胆)   : {csod['reason']} | top5: {top5s}")
        else:
            print(f"  cs_odds(波胆)   : {csod.get('reason', '不可用')}")
    cs = r["consensus"]
    if cs["model_avg_1x2"]:
        print("\n■ 一致性")
        print(f"  模型平均: 主{cs['model_avg_1x2'][0]*100:.1f}% 平{cs['model_avg_1x2'][1]*100:.1f}% "
              f"客{cs['model_avg_1x2'][2]*100:.1f}%   → 倾向: {cs['lean']}  (n={cs['n_models']})")
        if cs["vs_market_pp"]:
            print(f"  模型−市场(pp): 主{cs['vs_market_pp'][0]:+.1f} 平{cs['vs_market_pp'][1]:+.1f} "
                  f"客{cs['vs_market_pp'][2]:+.1f}")
    e = r["edge_1x2"]
    if e and e["edge_pp"] is not None:
        print("\n■ Edge 三件套 (+EV 判据: 胜率 > 隐含 + 抽水)")
        print(f"  {e['side']}: win_rate={e['win_rate']*100:.2f}%  implied={e['implied']*100:.2f}%  "
              f"edge={e['edge_pp']:+.2f}pp  → {'正EV方向' if e['edge_pp'] > 0 else '负EV, 结构分析对≠正EV'}")
    ou = r["ou"]
    if ou:
        print(f"\n■ OU: 模型P(大)={ou['model_p_over']*100:.1f}% vs 市场={ou['market_p_over']*100:.1f}% "
              f"→ {ou['lean']} (edge {ou['edge_pp']:+.2f}pp)")
    dr = r["drift"]
    if dr:
        print(f"\n■ 漂移(开盘→当前): 主{dr['drift_pp_home']:+.2f}pp 平{dr['drift_pp_draw']:+.2f}pp "
              f"客{dr['drift_pp_away']:+.2f}pp")
    lg = r["league_context"]
    if lg:
        print(f"\n■ 联赛背景 [{lg['league']}] n={lg['n']}: 主胜{lg['home_rate']*100:.1f}% "
              f"平{lg['draw_rate']*100:.1f}% 客胜{lg['away_rate']*100:.1f}% 场均{lg['avg_goals']:.2f}球")
    if r["honest_flags"]:
        print("\n■ 诚实边界")
        for f in r["honest_flags"]:
            print(f"  ⚠ {f}")
    print(f"\n耗时 {r['runtime_ms']}ms | 分析非预测, 不构成下注建议(IR-20)")


# ─────────────────────────────────────────────────────────────────
#  回测 (OOS: 时间外切分)
# ─────────────────────────────────────────────────────────────────
def backtest(league: str, n: int = 800, since: int = 2015) -> dict:
    """在 odds_features 历史数据上做赛前分析回测.
    预测用 close(临场/开赛前最后价) 去水 → 模型 argmax; 结算用 outcome.
    基线 = 去水 argmax(市场). 全部过滤假 0-0 (score 可信样本)."""
    try:
        import numpy as np
        from sklearn.metrics import accuracy_score, log_loss
    except Exception as e:
        return {"error": str(e)}
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT league, match_date, home_team, away_team, close_h, close_d, close_a, "
        "cimp_h, cimp_d, cimp_a, outcome, home_score, away_score FROM odds_features "
        "WHERE league LIKE ? AND match_date >= ? AND close_h>0 AND close_d>0 AND close_a>0 "
        "AND outcome IN ('H','D','A') ORDER BY match_date DESC LIMIT ?",
        (f"%{league}%", f"{since}-01-01", int(n))).fetchall()
    con.close()
    if len(rows) < 50:
        return {"error": f"样本不足({len(rows)}), 换联赛或放大 n"}

    # 假0-0过滤: 只有 0-0 且无法核实 score_at 的历史场次剔除(odds_features 无 score_at,
    # 用 0-0 且 outcome='D' 的高危子集剔除 — 本项目铁律优先干净)
    clean = [r for r in rows if not (r["home_score"] == 0 and r["away_score"] == 0)]
    n_raw, n_clean = len(rows), len(clean)
    rows = clean

    y = [0 if r["outcome"] == "H" else (1 if r["outcome"] == "D" else 2) for r in rows]
    y_np = np.array(y)
    base = np.array([np.argmax([r["cimp_h"], r["cimp_d"], r["cimp_a"]]) for r in rows])
    base_acc = float((base == y_np).mean())

    # 模型预测 (close 赔率 → fl + fused 三路对比)
    preds, model_ok = [], 0
    probas = []
    preds_fl, probas_fl = [], []
    for r in rows:
        fl = _fl_signals(h=r["close_h"], d=r["close_d"], a=r["close_a"], league=r["league"])
        fused = _fused_1x2(fl["1x2"], fl["ah"])
        p = fused or fl["1x2"]
        if p:
            preds.append(int(np.argmax(p)))
            probas.append(p)
            model_ok += 1
        else:
            preds.append(int(np.argmax([r["cimp_h"], r["cimp_d"], r["cimp_a"]])))
            probas.append([r["cimp_h"], r["cimp_d"], r["cimp_a"]])
        if fl["1x2"]:
            preds_fl.append(int(np.argmax(fl["1x2"])))
            probas_fl.append(fl["1x2"])
        else:
            preds_fl.append(int(np.argmax([r["cimp_h"], r["cimp_d"], r["cimp_a"]])))
            probas_fl.append([r["cimp_h"], r["cimp_d"], r["cimp_a"]])
    pred_np = np.array(preds)
    model_acc = float((pred_np == y_np).mean())
    fl_acc = float((np.array(preds_fl) == y_np).mean())
    proba_np = np.array(probas)
    # 规范化(模型概率可能不含水, 基线 cimp 已去水)
    proba_np = proba_np / proba_np.sum(1, keepdims=True)
    ll_model = float(log_loss(y_np, proba_np, labels=[0, 1, 2]))
    ll_fl = float(log_loss(y_np, np.array(probas_fl) / np.array(probas_fl).sum(1, keepdims=True),
                           labels=[0, 1, 2]))
    base_prob = np.array([[r["cimp_h"], r["cimp_d"], r["cimp_a"]] for r in rows])
    ll_base = float(log_loss(y_np, base_prob, labels=[0, 1, 2]))

    # 分边命中
    by_side = {}
    for i, nm in enumerate(("主胜", "平局", "客胜")):
        mk = y_np == i
        by_side[nm] = {"n": int(mk.sum()),
                       "model": float((pred_np[mk] == i).mean()) if mk.sum() else None,
                       "baseline": float((base[mk] == i).mean()) if mk.sum() else None}

    return {
        "league": league, "n_raw": n_raw, "n_clean": n_clean,
        "n_model_used": model_ok,
        "fused_acc": round(model_acc, 4), "fl_acc": round(fl_acc, 4),
        "baseline_acc": round(base_acc, 4),
        "uplift_pp": round((model_acc - base_acc) * 100.0, 2),
        "uplift_fl_pp": round((fl_acc - base_acc) * 100.0, 2),
        "logloss_fused": round(ll_model, 4), "logloss_fl": round(ll_fl, 4),
        "logloss_baseline": round(ll_base, 4),
        "by_side": by_side,
        "note": "预测用 close 价(开赛前最后), 结算用赛果; 假0-0已过滤; fused/fl 双路对比市场基线",
    }


# ─────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="哨响AI 世界级足球分析器")
    ap.add_argument("--home", default="皇家马德里")
    ap.add_argument("--away", default="巴塞罗那")
    ap.add_argument("--league", default="西甲")
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--d", type=float, default=None)
    ap.add_argument("--a", type=float, default=None)
    ap.add_argument("--ou-line", type=float, default=None)
    ap.add_argument("--ou-over", type=float, default=None)
    ap.add_argument("--ou-under", type=float, default=None)
    ap.add_argument("--ah-line", type=float, default=None)
    ap.add_argument("--ah-home", type=float, default=None)
    ap.add_argument("--ah-away", type=float, default=None)
    ap.add_argument("--op-h", type=float, default=None)
    ap.add_argument("--op-d", type=float, default=None)
    ap.add_argument("--op-a", type=float, default=None)
    ap.add_argument("--kickoff", default=None)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--backtest", action="store_true", help="回测模式")
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--since", type=int, default=2015)
    args = ap.parse_args(argv)

    if args.backtest:
        r = backtest(args.league, args.n, args.since)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return

    r = analyze_match(args.home, args.away, args.league,
                      h=args.h, d=args.d, a=args.a,
                      ou_line=args.ou_line, ou_over=args.ou_over, ou_under=args.ou_under,
                      ah_line=args.ah_line, ah_home=args.ah_home, ah_away=args.ah_away,
                      op_h=args.op_h, op_d=args.op_d, op_a=args.op_a,
                      kickoff=args.kickoff)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
