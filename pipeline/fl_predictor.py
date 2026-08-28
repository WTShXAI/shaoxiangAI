"""
哨响AI · 特征库模型预测器 (fl_predictor)
========================================
把外部输入的赔率结构，经 odds_feature_library.extract_features 压成 22 维特征，
再用 fl_model_{1x2,ou,ah}.joblib (DecisionTree, 由 GQ 结构库训练) 输出"正确选项"概率。

作为 ranked_predictor 的一路独立【结构信号】输入：
  - 纯由赔率结构推导，不依赖赛果、不依赖 WI/庄家去水主干。
  - 模型在乐鱼(单庄)GQ 数据上训练，与"跟热门"强相关；1X2/OU 信号弱于现有 SSoT，
    AH 有 +11.9pp 正信号。故默认【只透明展示、不参与融合】，由 ranked_predictor
    以 fl_structure_weight 受控开启融合(默认 0.0，零回归风险)。

类序 (与 train_feature_library_model 编码一致):
  1X2: [0,1,2] = [H, D, A]
  OU : [0,1]   = [O(大), U(小)]
  AH : [0,1]   = [H(主), A(客)]

接口: predict_from_odds(...) -> {"1x2":[p_h,p_d,p_a]|None, "ou":[p_over,p_under]|None, "ah":[p_home,p_away]|None}
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import numpy as np
from pipeline.odds_feature_library import extract_features, FEATURE_NAMES, N_FEAT
from pipeline.odds_structure_db import render_structure

MODEL_DIR = os.path.join(_ROOT, "data")
_MODEL_CACHE = {}


def _load(task: str):
    if task in _MODEL_CACHE:
        return _MODEL_CACHE[task]
    path = os.path.join(MODEL_DIR, f"fl_model_{task}.joblib")
    if not os.path.exists(path):
        _MODEL_CACHE[task] = None
        return None
    try:
        import joblib
        m = joblib.load(path)
        _MODEL_CACHE[task] = m
        return m
    except Exception:
        _MODEL_CACHE[task] = None
        return None


def predict_from_odds(h=None, d=None, a=None,
                      ou_line=None, ou_over=None, ou_under=None,
                      op_cs=None, ah_line=None, ah_home=None, ah_away=None,
                      league=None, kickoff=None) -> dict:
    """把一行赔率 → 渲染结构 → 提特征 → fl 模型概率。推理期无赛果。"""
    row = {
        "source": "live", "league": league, "kickoff": kickoff,
        "op_1x2_h": h, "op_1x2_d": d, "op_1x2_a": a,
        "op_ou_line": ou_line, "op_ou_over": ou_over, "op_ou_under": ou_under,
        "op_ah_line": ah_line, "op_ah_home": ah_home, "op_ah_away": ah_away,
        "op_cs": op_cs,
        # 推理期无赛果
        "score_home": None, "score_away": None, "result": None,
    }
    struct = render_structure(row)
    # league_freq: 推理期无训练分布, 传中性 0.0 (上下文弱特征)
    feat = extract_features(struct, 0.0, kickoff)
    X = np.array(feat, dtype=float).reshape(1, -1)

    out = {"1x2": None, "ou": None, "ah": None}

    m = _load("1x2")
    if m is not None:
        try:
            out["1x2"] = [float(x) for x in m.predict_proba(X)[0]]
        except Exception:
            out["1x2"] = None

    m = _load("ou")
    if m is not None:
        try:
            p = m.predict_proba(X)[0]
            out["ou"] = [float(p[0]), float(p[1])]   # [O, U]
        except Exception:
            out["ou"] = None

    m = _load("ah")
    if m is not None:
        try:
            p = m.predict_proba(X)[0]
            out["ah"] = [float(p[0]), float(p[1])]   # [H, A]
        except Exception:
            out["ah"] = None

    return out


def clear_cache():
    """测试/重训后清缓存，重新加载最新 fl_model_*.joblib。"""
    _MODEL_CACHE.clear()
