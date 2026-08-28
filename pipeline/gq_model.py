# ╔══════════════════════════════════════════════════════════════════════╗
# ║  ⚠ DEPRECATED — 2026-08-05 模型收敛 (M1-M7)                          ║
# ║  已下线: gq_1x2/gq_ou/gq_total 权重已归档                          ║
# ║  替代: M5 pipeline/fl_predictor.py (结构库模型)                      ║
# ║  单一真相源: pipeline/model_catalog.py                                ║
# ║  本文件保留仅为历史可追溯, 禁止在新代码中引用.                        ║
# ╚══════════════════════════════════════════════════════════════════════╝
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gq_model.py  —  乐鱼(GQ)单庄盘口数据训练的概率模型 (推理适配器)
------------------------------------------------------------------
定位: 用 GQ 采集器落地的"盘口+赛果"标注数据训练的模型。
  - 输入: match_outcomes 一行 (1X2/OU/AH/CS 赔率 + league/kickoff + 赛果)
  - 渲染"正确赔率"为特征值: 去水隐含概率 + 波胆结构 + 联赛频率 + 开球时段
  - 输出: P(H/D/A) / OU 大一小 / 期望总进球
  - 训练语料: data/events.db.match_outcomes (持续累积; 当前 ~2200 标注场)
  - 复用 william_inter_model 的接口风格 (build_features / predict_* / joblib)

诚实边界: GQ 为单庄(乐鱼), 去水隐含≈市场概率, 模型学到的是
"比原始隐含更稳的概率主干 + 盘口结构信号", 非跨庄 pick-edge。
"""
import os, json, math, joblib, warnings
import numpy as np

warnings.filterwarnings("ignore")

MODEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLS_PATH = os.path.join(MODEL_DIR, "data", "gq_1x2_model.joblib")
OU_PATH = os.path.join(MODEL_DIR, "data", "gq_ou_model.joblib")
TOT_PATH = os.path.join(MODEL_DIR, "data", "gq_total_model.joblib")
META_PATH = os.path.join(MODEL_DIR, "data", "gq_model_meta.json")

FEATURE_NAMES = [
    "imp_h", "imp_d", "imp_a",          # 1X2 去水隐含
    "imp_over", "imp_under", "ou_line",  # OU 去水隐含 + 盘口线
    "ah_p_home", "ah_p_away", "ah_line", # AH 去水隐含 + 让球线
    "cs_top1_prob", "cs_fav_prob", "cs_count", "cs_entropy",  # 波胆结构
    "league_freq", "kick_hour", "kick_dow",  # 联赛频率 + 开球时段
]


def _devig(*odds):
    try:
        inv = []
        for o in odds:
            try:
                inv.append(1.0 / o if (o and o > 1.01) else np.nan)
            except Exception:
                inv.append(np.nan)
        s = sum(x for x in inv if not math.isnan(x))
        if s <= 0 or math.isnan(s):
            return [np.nan] * len(odds)
        return [(x / s if not math.isnan(x) else np.nan) for x in inv]
    except Exception:
        return [np.nan] * len(odds)


def _safe(x):
    try:
        return float(x)
    except Exception:
        return np.nan


def build_features(row, league_freq=None):
    """把一行盘口数据渲染为特征值向量 (16维)。row 可为 dict / sqlite3.Row。"""
    h, d, a = _safe(row.get("op_1x2_h")), _safe(row.get("op_1x2_d")), _safe(row.get("op_1x2_a"))
    ih, id_, ia = (np.nan,) * 3
    if not any(math.isnan(x) for x in (h, d, a)):
        ih, id_, ia = _devig(h, d, a)

    ol, oo, ou = _safe(row.get("op_ou_line")), _safe(row.get("op_ou_over")), _safe(row.get("op_ou_under"))
    io, in_ = (np.nan,) * 2
    if not any(math.isnan(x) for x in (ol, oo, ou)):
        io, in_ = _devig(oo, ou)

    al, ah_, aa = _safe(row.get("op_ah_line")), _safe(row.get("op_ah_home")), _safe(row.get("op_ah_away"))
    ahph, ahpa = (np.nan,) * 2
    if not any(math.isnan(x) for x in (ah_, aa)):
        ahph, ahpa = _devig(ah_, aa)

    cs_top1, cs_fav, cs_cnt, cs_ent = np.nan, np.nan, 0.0, np.nan
    cs_raw = row.get("op_cs")
    if cs_raw and cs_raw not in ("[]", "", None):
        try:
            lst = json.loads(cs_raw) if isinstance(cs_raw, str) else cs_raw
            odds = [float(x[1]) for x in lst if isinstance(x, (list, tuple)) and len(x) >= 2 and _safe(x[1]) > 1.01]
            if odds:
                inv = [1.0 / o for o in odds]
                s = sum(inv)
                probs = [i / s for i in inv]
                cs_cnt = float(len(probs))
                cs_top1 = probs[0]
                cs_fav = max(probs)
                cs_ent = -sum(p * math.log(p) for p in probs if p > 0)
        except Exception:
            pass

    lf = league_freq.get(row.get("league"), 0.0) if league_freq else 0.0
    kh, kd = np.nan, np.nan
    ko = row.get("kickoff")
    try:
        import datetime as _dt
        if isinstance(ko, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = _dt.datetime.strptime(ko, fmt)
                    kh, kd = float(dt.hour), float(dt.weekday())
                    break
                except Exception:
                    continue
        elif ko:
            dt = _dt.datetime.utcfromtimestamp(float(ko))
            kh, kd = float(dt.hour), float(dt.weekday())
    except Exception:
        pass
    return [ih, id_, ia, io, in_, ol, ahph, ahpa, al,
            cs_top1, cs_fav, cs_cnt, cs_ent, lf, kh, kd]


class GQModel:
    def __init__(self):
        self.cls = None
        self.ou = None
        self.tot = None
        self.league_freq = {}
        self.meta = {}

    # ---- 训练 (由 scripts/train_gq_model.py 调用) ----
    def fit(self, X_cls, y_cls, X_ou, y_ou, X_tot, y_tot, league_freq, feat_idx, meta=None):
        # 单棵决策树(depth=3): 在"去水概率单纯形"输入上, 梯度提升(LightGBM/HistGBM)集体失效
        # (同特征 决策树59.6% vs 梯度提升42-45%), 仅单棵CART能学到"押热门"信号. 小样本下足矣.
        # feat_idx: {'1x2':[...], 'ou':[...], 'tot':[...]} 各目标使用的特征列索引, predict 时按此切片.
        from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor
        self.league_freq = league_freq or {}
        self.feat_idx = feat_idx or {"1x2": list(range(16)), "ou": list(range(16)), "tot": list(range(16))}
        self.cls = DecisionTreeClassifier(max_depth=3, min_samples_leaf=10, random_state=42)
        self.cls.fit(np.nan_to_num(np.asarray(X_cls, dtype=float)), np.asarray(y_cls))
        if X_ou is not None and len(X_ou) > 0:
            self.ou = DecisionTreeClassifier(max_depth=3, min_samples_leaf=10, random_state=42)
            self.ou.fit(np.nan_to_num(np.asarray(X_ou, dtype=float)), np.asarray(y_ou))
        if X_tot is not None and len(X_tot) > 0:
            self.tot = DecisionTreeRegressor(max_depth=3, min_samples_leaf=10, random_state=42)
            self.tot.fit(np.nan_to_num(np.asarray(X_tot, dtype=float)), np.asarray(y_tot))
        self.meta = meta or {}

    def save(self):
        if self.cls is not None:
            joblib.dump(self.cls, CLS_PATH)
        if self.ou is not None:
            joblib.dump(self.ou, OU_PATH)
        if self.tot is not None:
            joblib.dump(self.tot, TOT_PATH)
        meta = {"league_freq": self.league_freq, "features": FEATURE_NAMES,
                "feat_idx": self.feat_idx, **self.meta}
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    # ---- 推理 ----
    def _load(self):
        if self.cls is None:
            if os.path.exists(CLS_PATH):
                self.cls = joblib.load(CLS_PATH)
                self.ou = joblib.load(OU_PATH) if os.path.exists(OU_PATH) else None
                self.tot = joblib.load(TOT_PATH) if os.path.exists(TOT_PATH) else None
                if os.path.exists(META_PATH):
                    with open(META_PATH, encoding="utf-8") as f:
                        self.meta = json.load(f)
                    self.league_freq = self.meta.get("league_freq", {})
                    self.feat_idx = self.meta.get("feat_idx",
                        {"1x2": list(range(16)), "ou": list(range(16)), "tot": list(range(16))})

    def predict_1x2(self, row):
        self._load()
        if self.cls is None:
            return None
        f = build_features(row, self.league_freq)
        x = np.nan_to_num(np.array([[f[i] for i in self.feat_idx["1x2"]]], dtype=float))
        p = self.cls.predict_proba(x)[0]
        return {"H": float(p[0]), "D": float(p[1]), "A": float(p[2]),
                "cls": int(p.argmax()), "proba": [float(p[0]), float(p[1]), float(p[2])]}

    def predict_ou(self, row):
        self._load()
        if self.ou is None:
            return None
        f = build_features(row, self.league_freq)
        x = np.nan_to_num(np.array([[f[i] for i in self.feat_idx["ou"]]], dtype=float))
        p = self.ou.predict_proba(x)[0]  # [P(小), P(大)]
        return {"under": float(p[0]), "over": float(p[1]), "cls": int(p.argmax())}

    def predict_total(self, row):
        self._load()
        if self.tot is None:
            return None
        f = build_features(row, self.league_freq)
        x = np.nan_to_num(np.array([[f[i] for i in self.feat_idx["tot"]]], dtype=float))
        return float(np.clip(self.tot.predict(x)[0], 0, 15))


def load_model():
    m = GQModel()
    m._load()
    return m
