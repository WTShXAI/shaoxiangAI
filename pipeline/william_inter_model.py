#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
william_inter_model.py  —  新模型(威廉+Inter 历史训练)推理适配器
------------------------------------------------------------------
定位: 哨响AI 的"主导概率模型"。
  - 由 1X2 开盘/收盘赔率派生特征, 输出校准过的 P(H/D/A) 与期望总进球。
  - 训练语料: football_data.db 威廉(2012-2018)+Inter(2016-2025) 共 ~59.9万场,
    其中 1X2 命中率对"庄家热门"基线 -0.18pp (单庄无 pick-edge),
    但校准极佳(预测P(H)≈实际频率), 是系统最可靠的概率主干。
  - 接入方式: ranked_predictor 在拿到 1X2 收盘赔率时, 优先调用本模块的
    predict_1x2 / predict_total, 其输出权重最高(主导), 其他模型暂不动。

依赖: lightgbm, joblib, numpy, pandas (managed 3.13 已具备)
"""
import os, json, joblib, warnings
import numpy as np

warnings.filterwarnings("ignore")  # 抑制 LGBM/ sklearn 的 feature_names 提示

MODEL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLS_PATH = os.path.join(MODEL_DIR, "data", "wi_1x2_model.joblib")
TOT_PATH = os.path.join(MODEL_DIR, "data", "wi_total_model.joblib")
CALIB_PATH = os.path.join(MODEL_DIR, "data", "wi_calibrator.json")

FEATURES = ["open_h", "open_d", "open_a", "close_h", "close_d", "close_a",
            "close_overround", "imp_h", "imp_d", "imp_a",
            "open_overround", "imp_open_h", "imp_open_d", "imp_open_a",
            "drift_h", "drift_d", "drift_a", "ha_ratio", "draw_ratio", "fav_implied"]

_cls = None
_tot = None


def _load():
    global _cls, _tot
    if _cls is None:
        _cls = joblib.load(CLS_PATH)
    if _tot is None:
        _tot = joblib.load(TOT_PATH)


def derive_features(open_h, open_d, open_a, close_h, close_d, close_a):
    """与 build_william_inter_dataset.py 完全一致的派生逻辑。"""
    try:
        cor = 1.0 / close_h + 1.0 / close_d + 1.0 / close_a
        ih, id_, ia = (1.0 / close_h) / cor, (1.0 / close_d) / cor, (1.0 / close_a) / cor
    except Exception:
        cor, ih, id_, ia = np.nan, np.nan, np.nan, np.nan
    try:
        oor = 1.0 / open_h + 1.0 / open_d + 1.0 / open_a
        oih, oid, oia = (1.0 / open_h) / oor, (1.0 / open_d) / oor, (1.0 / open_a) / oor
    except Exception:
        oor, oih, oid, oia = np.nan, np.nan, np.nan, np.nan
    try:
        drift_h, drift_d, drift_a = close_h / open_h - 1, close_d / open_d - 1, close_a / open_a - 1
    except Exception:
        drift_h = drift_d = drift_a = np.nan
    try:
        ha_ratio = close_h / close_a
    except Exception:
        ha_ratio = np.nan
    try:
        draw_ratio = close_d / ((close_h + close_a) / 2.0)
    except Exception:
        draw_ratio = np.nan
    fav = max(ih, id_, ia) if not any(np.isnan(x) for x in (ih, id_, ia)) else np.nan
    return [open_h, open_d, open_a, close_h, close_d, close_a,
            cor, ih, id_, ia, oor, oih, oid, oia, drift_h, drift_d, drift_a,
            ha_ratio, draw_ratio, fav]


def predict_1x2(open_h, open_d, open_a, close_h, close_d, close_a):
    """返回 {'H':p, 'D':p, 'A':p, 'cls':argmax, 'proba':[p_h,p_d,p_a]}"""
    _load()
    x = np.array([derive_features(open_h, open_d, open_a, close_h, close_d, close_a)], dtype=float)
    p = _cls.predict_proba(x)[0]
    cls = int(p.argmax())
    return {"H": float(p[0]), "D": float(p[1]), "A": float(p[2]),
            "cls": cls, "proba": [float(p[0]), float(p[1]), float(p[2])]}


def predict_total(open_h, open_d, open_a, close_h, close_d, close_a):
    """返回期望总进球 (float)。"""
    _load()
    x = np.array([derive_features(open_h, open_d, open_a, close_h, close_d, close_a)], dtype=float)
    return float(np.clip(_tot.predict(x)[0], 0, 15))


def predict(open_h, open_d, open_a, close_h, close_d, close_a):
    """统一入口, 供 ranked_predictor 调用。"""
    r = predict_1x2(open_h, open_d, open_a, close_h, close_d, close_a)
    r["expected_total"] = predict_total(open_h, open_d, open_a, close_h, close_d, close_a)
    return r


# ── 蒸馏学生: per-class 温度缩放校准器 ──
_CALIB = None


def _load_calib():
    global _CALIB
    if _CALIB is None:
        try:
            with open(CALIB_PATH, "r", encoding="utf-8") as f:
                _CALIB = json.load(f)
        except Exception:
            _CALIB = None
    return _CALIB


def calibrate_devig(imp_h, imp_d, imp_a):
    """蒸馏学生: 仅用庄家隐含概率(=收盘赔率 devig) → 逼近教师 WI 的校准概率。

    输入 imp_* 已是**隐含概率**(devig 结果), 直接做 per-class 温度缩放,
    切勿再取倒数(devig). 这是 WI(教师) 知识迁移到轻量学生的落地:
    任意比赛只要有收盘赔率, 即可得到"WI 风格"的校准 1X2 概率,
    无需重建 WI 的全部历史特征。
    返回 [p_h, p_d, p_a]; 校准器缺失时退化为输入隐含概率 (不报错)。
    """
    cal = _load_calib()
    raw = np.array([float(imp_h), float(imp_d), float(imp_a)], dtype=float)
    s = raw.sum()
    if s > 0:
        raw = raw / s
    if cal is None:
        return [float(raw[0]), float(raw[1]), float(raw[2])]
    T = np.array([cal["T_h"], cal["T_d"], cal["T_a"]], dtype=float)
    x = np.log(np.clip(raw, 1e-6, 1)) / T
    x -= x.max()
    e = np.exp(x)
    stu = e / e.sum()
    return [float(stu[0]), float(stu[1]), float(stu[2])]


if __name__ == "__main__":
    # 自测
    sample = dict(open_h=2.10, open_d=3.40, open_a=3.50,
                  close_h=1.95, close_d=3.30, close_a=4.20)
    print("自测 odds:", sample)
    print("1X2:", predict_1x2(**sample))
    print("total:", round(predict_total(**sample), 2))
