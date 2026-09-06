# -*- coding: utf-8 -*-
"""
open_eye_predictor.py — 开盘天眼 推理模块 (部署期消费 independent_model_open_eye.joblib)

诚实定位:
  - 只输出"建议", 绝不自动下注 (IR-21 建仓/api/execute/confirm 人工审批)。
  - 不输出"稳赢/必中" 措辞 (IR-30 诚实边界); 只给 edge_pp / 1/4-Kelly 建议注码比例。
  - 特征严格无前视: 独立实力 as-of kickoff + 仅开盘派生 odds_extra(odds_open)。

特征源(双路, 均与训练语义一致):
  - 若该对阵在预建 indep_features 表有行(历史/已知场) -> 直接取, 与训练表逐位一致 (parity=0)。
  - 若无(未来场) -> compute_live_features(home,away,kickoff,league) (form 语义已修正,
        与 build_independent_features 同构; 未来场无同日历史, 无同日排序残差)。

输出:
  predict_1x2(home,away,open_h,open_d,open_a, kickoff, league) -> [p_h,p_d,p_a]
  recommend(...) -> 决策 dict (side / model_prob / market_implied / edge_pp / kelly_frac / tag)
"""
from __future__ import annotations
import os, json, ast, threading
import sqlite3
import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_PATH = os.path.join(_ROOT, "pipeline", "predictors", "saved_models", "independent_model_open_eye.joblib")
DB_PATH = os.path.join(_ROOT, "data", "football_data.db")
KELLY_FRACTION = 0.25          # 1/4 Kelly (框架合规)
KELLY_CAP = 0.10               # 单注封顶 10% 本金
MIN_EDGE_PP = 0.0             # 仅 edge>0 才建议; 监控侧再加更高阈值

INDEP_FEATS = ["elo_home", "elo_away", "elo_diff", "form_home", "form_away", "form_diff",
               "rest_home", "rest_away", "rest_diff", "h2h_home_win", "h2h_draw",
               "h2h_away_win", "league_strength"]
ODDS_OPEN_FEATS = ["devig_h", "devig_d", "devig_a", "overround", "lambda_home",
                   "lambda_away", "dc_draw", "draw_dev", "entropy", "margin_impl"]
FEATURES = INDEP_FEATS + ODDS_OPEN_FEATS

_lock = threading.Lock()
_model_meta = None
_alias_cache = {}


def _load_model():
    global _model_meta
    if _model_meta is None:
        import joblib
        _model_meta = joblib.load(MODEL_PATH)
    return _model_meta


def build_alias_map(cur):
    amap = {}
    for canon, aj in cur.execute("SELECT canonical, aliases_json FROM team_canonical"):
        amap[canon.strip().lower()] = canon
        if aj:
            try:
                for a in (json.loads(aj) if aj.strip().startswith("[") else ast.literal_eval(aj)):
                    amap[str(a).strip().lower()] = canon
            except Exception:
                pass
    return amap


def canon(team):
    """与 build_independent_features / indep_features_runtime 同构: 小写 -> canonical。"""
    if not team:
        return None
    t = str(team).strip().lower()
    # amap 在 _build_prebuilt_index 中构建并缓存
    am = _alias_cache.get("amap")
    if am is not None:
        return am.get(t, str(team).strip())
    return str(team).strip()


def _ensure_amap():
    """加载 team_canonical 别名映射(覆盖门 + canon 一致性所需), 缓存。"""
    global _alias_cache
    if _alias_cache.get("amap") is None:
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            _alias_cache["amap"] = build_alias_map(con)
        finally:
            con.close()
    return _alias_cache["amap"]


def _features(home, away, match_date, league):
    """返回 13 维独立实力。

    统一经 compute_live_features(与 build_independent_features 同 canon/同逻辑):
      - 历史/已知场: 命中 emitted 字典, 与训练期预建 indep_features 表逐位一致 (parity=0)。
      - 未来场: 由历史终态推导赛前特征(无同日历史, 无同日序残差)。
    弃用旧的 _prebuilt_idx 分支——其 canon 与 build 不一致, 会把历史场误导向未来特征路径。
    """
    from pipeline.predictors.indep_features_runtime import compute_live_features
    cl = compute_live_features(home, away, str(match_date)[:10], league or "", DB_PATH)
    return [float(cl[c]) for c in INDEP_FEATS]


def _pois(l, k):
    if k < 0:
        return 0.0
    return math.exp(-l) * (l ** k) / math.factorial(k)


def _dc_probs(lh, la, n=12):
    H = D = A = 0.0
    for i in range(n + 1):
        for j in range(n + 1):
            p = _pois(lh, i) * _pois(la, j)
            H += p if i > j else (D if i == j else A)
    return H, D, A


def _dc_inv(ph, pd, pa, iters=80, lr=0.08):
    lh, la = 1.4, 1.1
    for _ in range(iters):
        H, D, A = _dc_probs(lh, la)
        lh -= lr * (H - ph); la -= lr * (A - pa)
        lh = max(0.05, min(lh, 6.0)); la = max(0.05, min(la, 6.0))
    return lh, la


def odds_extra(oh, od, oa):
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    dh, dd, da = (1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv
    lh, la = _dc_inv(dh, dd, da)
    _, dcD, _ = _dc_probs(lh, la)
    ent = sum(-p * math.log(p) for p in (dh, dd, da) if p > 0)
    return [dh, dd, da, inv - 1.0, lh, la, dcD, dd - dcD, ent, dh - da]


import math


def _covered(home, away):
    """覆盖门(真实可下注边界, 非数据窥探):
    天眼 edge 来自独立实力特征(Elo/form/rest/h2h), 仅对'两队均有可靠历史追踪'的比赛有效。
    两队都必须出现在 team_canonical 映射中; 任一为未知/冷门队 -> 无可靠特征 -> 天眼 PASS。
    实证: 两队已知 n=1916 ROI=+17.58%(CI正); 含未知队 n=5597 ROI=-2.11%(CI跨零)。"""
    _ensure_amap()
    am = _alias_cache.get("amap") or {}
    h = str(home).strip().lower() in am
    a = str(away).strip().lower() in am
    return bool(h and a)


def predict_1x2(home, away, open_h, open_d, open_a, kickoff=None, league=None) -> list:
    """返回 [p_h, p_d, p_a] 或 None(异常 -> 零回归)。"""
    try:
        meta = _load_model()
        indep = _features(home, away, kickoff or "", league)
        oe = odds_extra(float(open_h), float(open_d), float(open_a))
        X = np.array([indep + oe], dtype=np.float64)
        proba = meta["model"].predict_proba(X)[0]
        return [float(x) for x in proba]
    except Exception:
        return None


def kelly_fraction(prob, odds, fraction=KELLY_FRACTION, cap=KELLY_CAP):
    """1/4 Kelly, 单注封顶; 负 edge 返回 0。"""
    if prob is None or odds is None or odds <= 1.0:
        return 0.0
    f_star = (prob * (odds - 1.0) - (1.0 - prob)) / odds
    if f_star <= 0:
        return 0.0
    return round(min(fraction * f_star, cap), 4)


def recommend(home, away, open_h, open_d, open_a, kickoff=None, league=None) -> dict:
    """开盘天眼 +EV 决策(仅建议, 不自动下注)。

    策略: 押 模型P - 开盘去水隐含P 最大的一方(= EYE_OPEN_RESID, OOF +17.4%);
          要求 edge_pp>0 且覆盖达标 才输出建议。返回 side/model_prob/market_implied/edge_pp/kelly_frac。
    """
    indep = _features(home, away, kickoff or "", league)
    if not _covered(home, away):
        return {"ok": False, "reason": "覆盖不足(至少一队无可靠独立实力历史), 天眼 PASS (IR-30 诚实边界)"}
    proba = predict_1x2(home, away, open_h, open_d, open_a, kickoff, league)
    if proba is None:
        return {"ok": False, "reason": "特征/模型不可用(零回归)"}
    oh, od, oa = float(open_h), float(open_d), float(open_a)
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    impl = [(1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv]
    side_idx = int(np.argmax([proba[i] - impl[i] for i in range(3)]))
    side = ("H", "D", "A")[side_idx]
    odds = (oh, od, oa)[side_idx]
    model_prob = proba[side_idx]
    market_implied = impl[side_idx]
    edge_pp = round(100 * (model_prob - market_implied), 2)
    kf = kelly_fraction(model_prob, odds) if edge_pp > MIN_EDGE_PP else 0.0
    return {
        "ok": True,
        "side": side,
        "model_prob": round(model_prob, 4),
        "market_implied": round(market_implied, 4),
        "edge_pp": edge_pp,
        "odds": odds,
        "kelly_frac": kf,
        "prob_hda": [round(p, 4) for p in proba],
        "compliant": "建议仅; 需人工审批(IR-21) 且不得标注稳赢(IR-30)",
    }


if __name__ == "__main__":
    # 自测: 取一支近期历史场(预建表有行), 验证 recommend 输出 +EV 结构
    con = sqlite3.connect(DB_PATH)
    r = con.execute(
        "SELECT m.home_team_name, m.away_team_name, m.match_date, m.league_name, "
        "mf.odds_open_h, mf.odds_open_d, mf.odds_open_a FROM matches m JOIN match_features mf "
        "ON m.match_id=mf.match_id WHERE m.final_result IN ('H','D','A') AND mf.odds_open_h>0 "
        "AND mf.odds_open_d>0 AND mf.odds_open_a>0 AND m.match_date>='2023-06-01' LIMIT 1").fetchone()
    con.close()
    h, a, md, lg, oh, od, oa = r
    print(f"self-test {h} vs {a} @ {md} [{lg}]  open={oh}/{od}/{oa}")
    print(json.dumps(recommend(h, a, oh, od, oa, md, lg), ensure_ascii=False, indent=2))
