"""Poisson GBM 进球模型 — 生产封装 (2026-08-30)。

训练: scripts/train_poisson_goals_20260830.py
      football_data.db `odds_features` 30.3 万场(剔除 2026 坏数据),
      XGBoost count:poisson 分别预测 λ_h / λ_a。

用途: 从 λ_h/λ_a 的 Dixon-Coles 联合分布一次性导出
      · 方向概率 P(主/平/客)
      · 比分分布 top-N
      · 大小球 P(总进球 > line)

⚠ 训练特征来自 football_data.db 的 odds_features; 生产要读 events.db,
   必须在 `build_features()` 里构造**同构**特征, 缺的字段(home_edge/sigma_trap)
   填 0 —— 与训练时 fillna(0) 行为一致, 不引入分布偏移。

⚠ 已实测结论(赛前): 市场去水 argmax 52.40% > 本模型 52.29%。
   **赛前场景市场已接近最优, 不要指望替换后赛前准确率提升**;
   本模型的价值在滚球条件化(λ 作为剩余时间进球强度的起点)。
"""
from __future__ import annotations

import math
import os
import threading
from typing import Dict, List, Optional, Tuple

import joblib
import numpy as np

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(_HERE, "models", "poisson_goals_20260830.joblib")
MAX_GOALS = 10
EPS = 1e-6

# 联赛统计缺失时的全局默认(训练集中位数附近)
_DEFAULT_LG = {"lg_hg": 1.40, "lg_ag": 1.10, "lg_draw": 0.25}
# 训练数据截至 2025; 生产传 2026 会外推, 故钉在最后一年
FEAT_YEAR = 2025

_lock = threading.Lock()
_BUNDLE = None
# ⚠ 默认**关闭** —— 2026-08-30 实测(滚球场景, 干净子集 2247 个采样点):
#   OIP λ (现生产)  方向 68.7% | top1 21.9% | top3 52.9% | Brier 0.7086
#   GBM λ (本模型)  方向 64.9% | top1 20.0% | top3 51.1% | Brier 0.7197
#   GBM λ×1.2 校准  方向 66.4% (缩放有改善但仍不及 OIP)
#   融合 w*OIP+(1-w)*GBM 呈**单调**: w 越大越好, 纯 OIP 最优。
#   根因: GBM 的 λ 系统性偏低约 9%(平均总 λ 2.273 vs OIP 2.500),
#         正则化收缩 + 训练数据均值偏低, 且 Brier 全面更差。
# → 启用会导致准确率退步 3.8pp, 故默认关闭; 仅作交叉验证/实验用。
#   复现: runpy scripts/compare_inplay_lambda_20260830.py
#
# 2026-08-30 用户拍板: 滚球进球后赔率大幅缩水、无实际下注价值, **只有赛前分析有价值**
#   → OIP 权重调到最大(1.0), 直到有新模型按 config/model_weights.json 的
#     promotion_gate 验收通过才调低。权重改由配置控制, 不再硬编码。
ENABLED = False

# λ 来源权重 (读 config/model_weights.json; OIP 默认拉满)
_WEIGHTS = {"oip": 1.0, "gbm": 0.0, "loaded": False}


def _weights():
    if _WEIGHTS["loaded"]:
        return _WEIGHTS
    _WEIGHTS["loaded"] = True
    try:
        import json
        p = os.path.join(_HERE, "config", "model_weights.json")
        with open(p, encoding="utf-8") as f:
            ls = (json.load(f).get("lambda_source") or {})
        _WEIGHTS["oip"] = float(ls.get("oip_weight", 1.0))
        _WEIGHTS["gbm"] = float(ls.get("gbm_weight", 0.0))
    except Exception:
        pass
    return _WEIGHTS


def lambdas_weighted(lam_oip, lam_gbm):
    """按配置权重混合两个 λ 来源。OIP 权重=1 时即纯 OIP（当前状态）。"""
    w = _weights()
    wo, wg = w["oip"], w["gbm"]
    s = wo + wg
    if s <= 0:
        return lam_oip
    return (wo / s * lam_oip[0] + wg / s * lam_gbm[0],
            wo / s * lam_oip[1] + wg / s * lam_gbm[1])


def set_enabled(v: bool) -> None:
    """开关: True 时用 GBM λ 替代 OIP λ（**实测会退步，默认关闭**）。"""
    global ENABLED
    ENABLED = bool(v)


def is_enabled() -> bool:
    return bool(ENABLED) and available()


def _load():
    global _BUNDLE
    if _BUNDLE is None:
        with _lock:
            if _BUNDLE is None:
                try:
                    _BUNDLE = joblib.load(MODEL_PATH)
                except Exception as e:  # 模型缺失 → 全链路静默降级
                    _BUNDLE = {"error": f"{type(e).__name__}: {e}"}
    return _BUNDLE


def available() -> bool:
    b = _load()
    return bool(b) and "error" not in b and b.get("mh") is not None


def _dewater(h: float, d: float, a: float) -> Tuple[float, float, float]:
    """赔率 → 去水概率。"""
    try:
        ih, id_, ia = 1.0 / h, 1.0 / d, 1.0 / a
    except Exception:
        return (1 / 3, 1 / 3, 1 / 3)
    s = ih + id_ + ia
    if s <= 0:
        return (1 / 3, 1 / 3, 1 / 3)
    return (ih / s, id_ / s, ia / s)


def build_features(oh: float, od: float, oa: float,
                   ch: Optional[float] = None, cd: Optional[float] = None,
                   ca: Optional[float] = None,
                   league: Optional[str] = None) -> Optional[np.ndarray]:
    """构造与训练同构的 30 维特征。

    oh/od/oa: 开盘 1X2 赔率; ch/cd/ca: 当前(最新)1X2 赔率, 缺失则用开盘代替。
    """
    b = _load()
    if not available():
        return None
    feats: List[str] = b["features"]
    ch = ch if (ch and ch > 1.01) else oh
    cd = cd if (cd and cd > 1.01) else od
    ca = ca if (ca and ca > 1.01) else oa

    ih, id_, ia = 1.0 / oh, 1.0 / od, 1.0 / oa
    sh = ih + id_ + ia
    cimp_h, cimp_d, cimp_a = _dewater(oh, od, oa)
    imp_h, imp_d, imp_a = ih / sh, id_ / sh, ia / sh
    overround = sh - 1.0
    drift_h, drift_d, drift_a = ch - oh, cd - od, ca - oa

    lg = _DEFAULT_LG.copy()
    lstats = b.get("league_stats")
    if league and lstats is not None:
        try:
            row = lstats.loc[league]
            lg = {"lg_hg": float(row["lg_hg"]), "lg_ag": float(row["lg_ag"]),
                  "lg_draw": float(row["lg_draw"])}
        except Exception:
            pass  # 联赛未命中 → 全局默认

    vals = {
        "cimp_h": cimp_h, "cimp_d": cimp_d, "cimp_a": cimp_a,
        "imp_h": imp_h, "imp_d": imp_d, "imp_a": imp_a,
        "open_h": oh, "open_d": od, "open_a": oa,
        "close_h": ch, "close_d": cd, "close_a": ca,
        "drift_h": drift_h, "drift_d": drift_d, "drift_a": drift_a,
        "overround": overround,
        "home_edge": 0.0,          # events.db 无此字段 → 与训练 fillna(0) 一致
        "sigma_trap": 0.0,         # 同上
        "f_ratio_ha": cimp_h / (cimp_a + EPS),
        "f_diff_ha": cimp_h - cimp_a,
        "f_draw_gap": cimp_d - (cimp_h + cimp_a) / 2.0,
        "f_open_ratio": oh / (oa + EPS),
        "f_close_ratio": ch / (ca + EPS),
        "f_open_close_h": ch - oh,
        "f_abs_drift": max(abs(drift_h), abs(drift_d), abs(drift_a)),
        "f_fav": max(cimp_h, cimp_d, cimp_a),
        "f_entropy": -(cimp_h * math.log(cimp_h + EPS)
                       + cimp_d * math.log(cimp_d + EPS)
                       + cimp_a * math.log(cimp_a + EPS)),
        "lg_hg": lg["lg_hg"], "lg_ag": lg["lg_ag"], "lg_draw": lg["lg_draw"],
        "f_year": FEAT_YEAR,
    }
    return np.array([[float(vals.get(f, 0.0)) for f in feats]])


def predict_lambdas(oh: float, od: float, oa: float,
                    ch: Optional[float] = None, cd: Optional[float] = None,
                    ca: Optional[float] = None,
                    league: Optional[str] = None) -> Optional[Tuple[float, float]]:
    """→ (λ_h, λ_a)；模型不可用时返回 None（调用方应回退现有逻辑）。"""
    if not available():
        return None
    X = build_features(oh, od, oa, ch, cd, ca, league)
    if X is None:
        return None
    try:
        b = _load()
        lh = float(b["mh"].predict(X)[0])
        la = float(b["ma"].predict(X)[0])
    except Exception:
        return None
    return (min(max(lh, 0.05), 6.0), min(max(la, 0.05), 6.0))


# ── Dixon-Coles 联合分布 ──
def _tau(x: int, y: int, lh: float, la: float, rho: float) -> float:
    if x == 0 and y == 0:
        return 1.0 - lh * la * rho
    if x == 0 and y == 1:
        return 1.0 + la * rho
    if x == 1 and y == 0:
        return 1.0 + lh * rho
    if x == 1 and y == 1:
        return 1.0 - rho
    return 1.0


def _pmf(lam: float, k: int) -> float:
    lam = float(np.clip(lam, 1e-6, 20.0))
    return math.exp(-lam) * lam ** k / math.factorial(k)


def joint_distribution(lh: float, la: float) -> Dict[str, float]:
    """归一化联合分布 {"i-j": p}（含 Dixon-Coles 修正）。"""
    b = _load()
    rho = float((b or {}).get("dc_rho") or 0.0)
    g: Dict[str, float] = {}
    for i in range(MAX_GOALS + 1):
        pi = _pmf(lh, i)
        if pi < 1e-8 and i > 7:
            break
        for j in range(MAX_GOALS + 1):
            pj = _pmf(la, j)
            if pj < 1e-8 and j > 7:
                break
            p = pi * pj * _tau(i, j, lh, la, rho)
            if p > 0:
                g[f"{i}-{j}"] = p
    s = sum(g.values())
    return {k: v / s for k, v in g.items()} if s > 0 else g


def score_topn(lh: float, la: float, n: int = 3) -> List[Dict]:
    g = joint_distribution(lh, la)
    top = sorted(g.items(), key=lambda x: -x[1])[:n]
    return [{"score": k, "prob": round(v, 4)} for k, v in top]


def direction_probs(lh: float, la: float) -> Tuple[float, float, float]:
    """→ (P主胜, P平, P客胜)。独立泊松会低估平局，故沿用训练期的平局 boost。"""
    g = joint_distribution(lh, la)
    ph = pd_ = pa = 0.0
    for k, p in g.items():
        i, j = (int(x) for x in k.split("-"))
        if i > j:
            ph += p
        elif i == j:
            pd_ += p
        else:
            pa += p
    b = _load()
    boost = float((b or {}).get("draw_boost") or 1.0)
    pd_ *= boost
    s = ph + pd_ + pa
    return (ph / s, pd_ / s, pa / s) if s > 0 else (1 / 3, 1 / 3, 1 / 3)


def p_over(lh: float, la: float, line: float) -> float:
    """P(总进球 > line)。"""
    g = joint_distribution(lh, la)
    return min(max(sum(p for k, p in g.items()
                       if sum(int(x) for x in k.split("-")) > line), 0.0), 1.0)


# ── 生产统一入口 ──
def lambdas_for(oh: float, od: float, oa: float,
                ch: Optional[float] = None, cd: Optional[float] = None,
                ca: Optional[float] = None,
                league: Optional[str] = None,
                oip_fallback=None) -> Optional[Tuple[float, float]]:
    """λ 统一入口: 开关开启时返回 GBM λ, 否则调用 oip_fallback 走现生产 OIP。

    oip_fallback: 形如 callable(oh, od, oa, league) -> (λ_h, λ_a)
    """
    if is_enabled():
        r = predict_lambdas(oh, od, oa, ch, cd, ca, league)
        if r:
            return r
    if callable(oip_fallback):
        try:
            return oip_fallback(oh, od, oa, league)
        except Exception:
            pass
    return None


# ── OU: 校准后的 P(over) ──
# 实测(时间外 n=926, 干净子集):
#   naive 市场隐含     Brier 0.2499 | LogLoss 0.6929 | AUC 0.5153 | ROI  0.00%
#   GBM 原始(未校准)   Brier 0.2866 | LogLoss 0.7822 | AUC 0.5774 | ROI -4.46%
#   **GBM + Platt**    Brier 0.2457 | LogLoss 0.6845 | AUC 0.5774 | ROI +8.89%
#   (旧 ou_opening_model.json: AUC 0.5076 / ROI -5.60%)
# → 校准后三项全优于市场, 且远胜旧模型。**OU 应改用本函数**。
_CAL_PATH = os.path.join(_HERE, "models", "ou_calibrator_20260830.joblib")
_cal_cache = {"ts": 0.0, "obj": None, "loaded": False}


def _calibrator():
    import time as _t
    if not _cal_cache["loaded"]:
        _cal_cache["loaded"] = True
        try:
            _cal_cache["obj"] = joblib.load(_CAL_PATH)
            _cal_cache["ts"] = _t.time()
        except Exception:
            _cal_cache["obj"] = None
    return _cal_cache["obj"]


def p_over_calibrated(lh: float, la: float, line: float) -> Optional[float]:
    """校准后的 P(总进球 > line)。校准器缺失时返回 None（调用方回退）。"""
    raw = p_over(lh, la, line)
    cal = _calibrator()
    if not cal or cal.get("logreg") is None:
        return None
    try:
        import math as _m
        p = min(max(raw, 1e-4), 1 - 1e-4)
        z = _m.log(p / (1 - p))
        out = float(cal["logreg"].predict_proba([[z]])[0][1])
        return min(max(out, 0.01), 0.99)
    except Exception:
        return None


def p_over_best(lh: float, la: float, line: float, market_implied: float):
    """OU 统一入口: 优先校准后的 GBM 概率, 失败则回退市场隐含概率。"""
    p = p_over_calibrated(lh, la, line)
    return p if (p is not None) else market_implied
