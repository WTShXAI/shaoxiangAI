"""
independent_predictor.py — 推理端消费 independent_model.joblib (M1 接入层)

诚实定位: 本文件不训练、不重造模型, 只是把 train_independent_model.py 产出的
independent_model.joblib 在预测期"调起来"。特征构造(odds_extra)与概率合并(combine)
严格复刻训练脚本, 顺序以 joblib 内 feat_cols 为准(不硬编码计数)。

设计铁律(沿用 ranked_predictor 零回归守卫):
  - 任何异常/查不到特征 → 返回 None, 由 blend_1x2 / 调用方自动忽略(零回归)。
  - 不修改既有概率链路, 只作为一路可选组件注入。
"""
from __future__ import annotations
import os
import json
import math
import sqlite3
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(_ROOT, "pipeline", "predictors", "saved_models", "independent_model.joblib")
DB_PATH = os.path.join(_ROOT, "data", "football_data.db")

_lock = threading.Lock()
_model_meta = None
_pair_index: Optional[Dict[Tuple[str, str], dict]] = None


def _load_model():
    global _model_meta
    if _model_meta is None:
        import joblib
        _model_meta = joblib.load(MODEL_PATH)
    return _model_meta


def _build_pair_index():
    """在 indep_features 自身词表内建 (小写home, 小写away) -> 最新一行 的索引.
    不依赖 team_canonical(其规范化会把队名重映射成不同拼写, 反而破坏配对).
    大小写不敏感匹配即可覆盖推理期队名的大小写差异."""
    global _pair_index
    if _pair_index is not None:
        return _pair_index
    idx: Dict[Tuple[str, str], dict] = {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(indep_features)")]
        for rec in conn.execute("SELECT * FROM indep_features"):
            row = {cols[i]: rec[i] for i in range(len(cols))}
            h = str(row.get("home", "")).strip().lower()
            a = str(row.get("away", "")).strip().lower()
            key = (h, a)
            # 同一对阵多条时保留较新(match_date 较大)的一条
            prev = idx.get(key)
            if prev is None or str(row.get("match_date", "")) >= str(prev.get("match_date", "")):
                idx[key] = row
        conn.close()
    except Exception:
        idx = {}
    _pair_index = idx
    return idx


def _lookup_indep(home: str, away: str) -> Optional[dict]:
    """在 indep_features 自身词表(大小写不敏感)查该对阵的最新独立特征. 查不到返回 None."""
    idx = _build_pair_index()
    key = (str(home).strip().lower(), str(away).strip().lower())
    return idx.get(key)


# ── 以下 odds_extra / combine 严格复刻 train_independent_model.py ──
def _pois(l, k):
    if k < 0:
        return 0.0
    import math
    return math.exp(-l) * (l ** k) / math.factorial(k)


def _dc_probs(lh, la, n=12):
    H = D = A = 0.0
    for i in range(n + 1):
        for j in range(n + 1):
            p = _pois(lh, i) * _pois(la, j)
            if i > j:
                H += p
            elif i == j:
                D += p
            else:
                A += p
    return H, D, A


def _dc_inv(ph, pd, pa, iters=80, lr=0.08):
    lh, la = 1.4, 1.1
    for _ in range(iters):
        H, D, A = _dc_probs(lh, la)
        lh -= lr * (H - ph)
        la -= lr * (A - pa)
        lh = max(0.05, min(lh, 6.0))
        la = max(0.05, min(la, 6.0))
    return lh, la


def odds_extra(oh, od, oa) -> Dict[str, float]:
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    dh, dd, da = (1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv
    lh, la = _dc_inv(dh, dd, da)
    _, dcD, _ = _dc_probs(lh, la)
    ent = 0.0
    for p in (dh, dd, da):
        if p > 0:
            ent -= p * math.log(p)
    return {
        "odds_close_h": oh, "odds_close_d": od, "odds_close_a": oa,
        "devig_h": dh, "devig_d": dd, "devig_a": da, "overround": inv - 1.0,
        "lambda_home": lh, "lambda_away": la, "dc_draw": dcD,
        "draw_dev": dd - dcD, "entropy": ent, "margin_impl": dh - da,
    }


def _combine(main_proba, draw_proba):
    pd_exp = draw_proba[:, 1]
    ph, pa = main_proba[:, 0], main_proba[:, 2]
    s = ph + pa
    phn = np.where(s > 0, ph / s, 0.5)
    pan = np.where(s > 0, pa / s, 0.5)
    return np.stack([(1 - pd_exp) * phn, pd_exp, (1 - pd_exp) * pan], axis=1)


def predict_1x2(home: str, away: str, h: float, d: float, a: float) -> Optional[List[float]]:
    """返回 [p_h, p_d, p_a] 或 None(查不到/异常 → 零回归)."""
    try:
        meta = _load_model()
        indep_feats = set(meta.get("indep_feats", []))
        feat_cols = meta["feat_cols"]
        indep_row = _lookup_indep(home, away)
        if indep_row is None:
            return None
        oe = odds_extra(float(h), float(d), float(a))
        X = []
        for c in feat_cols:
            if c in indep_feats:
                try:
                    X.append(float(indep_row[c]))
                except Exception:
                    X.append(0.0)
            else:
                X.append(float(oe[c]))
        X = np.array([X], dtype=np.float64)
        main_p = meta["model_main"].predict_proba(X)
        draw_p = meta["model_draw"].predict_proba(X)
        out = _combine(main_p, draw_p)[0]
        return [float(x) for x in out]
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    sys.path.insert(0, _ROOT)
    # 自测: 用一支真实有数据的对阵(取 indep_features 最新一对)
    conn = sqlite3.connect(DB_PATH)
    r = conn.execute("SELECT home, away FROM indep_features ORDER BY match_date DESC LIMIT 1").fetchone()
    conn.close()
    h, a = r[0], r[1]
    res = predict_1x2(h, a, 2.1, 3.3, 3.1)
    print(f"self-test {h} vs {a}: {res}")
