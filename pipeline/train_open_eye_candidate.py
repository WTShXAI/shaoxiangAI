# -*- coding: utf-8 -*-
"""
train_open_eye_candidate — 验证门控进化引擎的"重训"半环 (哨响AI, 2026-09-01)

诚实边界 (IR-30): 本脚本只生产候选权重 + 训练切点元数据, 绝不自举晋升。
晋升必须经 evolution.py --verify / --promote 五道关 (含 G4 真 OOS: oos_min_date
须晚于本脚本写入的 trained_on 切点)。

做法: 严格复用 open_eye_predictor 的 as-of-kickoff 特征管线 (_features / odds_extra),
与 incumbent 逐位一致 (parity=0, 不引入新泄漏)。训练窗口 = match_date < TRAIN_CUTOFF
(默认 2018-01-01; 之后覆盖人口 5876 行作真 OOS 验证)。标签 final_result H/D/A -> 0/1/2
(与 pipeline.evaluation.metrics._outcome_idx 一致: {"H":0,"D":1,"A":2})。

超参: 镜像 incumbent (LGBMClassifier.get_params), 保证与现任同构、可比。
"""
from __future__ import annotations

import os
import sys
import json
import sqlite3
import argparse
from typing import Tuple

import numpy as np

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline import open_eye_predictor as oe  # reuse _features/odds_extra/_covered (parity=0)
from pipeline.evaluation import metrics as M  # reuse devig/accuracy for honest self-check

DB_PATH = oe.DB_PATH
CANDIDATES_DIR = os.path.join(_ROOT, "pipeline", "predictors", "candidates")
INCUMBENT_PATH = oe.MODEL_PATH
TRAIN_CUTOFF_DEFAULT = "2018-01-01"
LABEL = {"H": 0, "D": 1, "A": 2}


def build_dataset(cutoff: str) -> Tuple[np.ndarray, np.ndarray, int, int]:
    """取 match_date < cutoff 的 covered+clean 行, 复用 open_eye 特征管线构 X/y。"""
    con = sqlite3.connect(DB_PATH, timeout=30)
    try:
        rows = con.execute(
            """SELECT m.home_team_name, m.away_team_name, m.match_date, m.league_name,
                      mf.odds_open_h, mf.odds_open_d, mf.odds_open_a, m.final_result
               FROM matches m JOIN match_features mf ON m.match_id=mf.match_id
               WHERE m.final_result IN ('H','D','A') AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL
                 AND mf.odds_open_h>0 AND mf.odds_open_d>0 AND mf.odds_open_a>0
                 AND m.match_date < ?
               ORDER BY m.match_date ASC""", (cutoff,)).fetchall()
    finally:
        con.close()
    X, y = [], []
    skipped = 0
    for h, a, md, lg, oh, od, oa, fr in rows:
        if not oe._covered(h, a):
            continue
        try:
            indep = oe._features(h, a, md, lg)
            oe_vec = oe.odds_extra(float(oh), float(od), float(oa))
        except Exception:
            skipped += 1
            continue
        X.append(indep + oe_vec)
        y.append(LABEL[fr])
    return np.array(X, dtype=np.float64), np.array(y, dtype=int), len(rows), skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="开盘天眼候选重训 (验证门控进化半环)")
    ap.add_argument("--cutoff", default=TRAIN_CUTOFF_DEFAULT, help="训练切点: match_date < 此日期")
    ap.add_argument("--out", default="", help="候选输出路径 (默认 candidates/ 下)")
    ap.add_argument("--note", default="", help="备注")
    args = ap.parse_args()

    X, y, raw, skipped = build_dataset(args.cutoff)
    print(f"[build] raw(pre-cutoff)={raw} covered_kept={len(X)} skipped(uncovered/feat_err)={skipped}")
    if len(X) < 500:
        print("ERROR: 训练样本不足 500, 中止 (IR-30: 样本不足不臆造模型)"); return 1

    # 镜像 incumbent 超参 (与现任同构、可比)
    inc = oe._load_model()
    base_params = inc["model"].get_params() if hasattr(inc["model"], "get_params") else {}
    from lightgbm import LGBMClassifier
    allowed = set(LGBMClassifier().get_params().keys())
    params = {k: v for k, v in base_params.items() if k in allowed}
    print(f"[train] LGBM params(镜像incumbent): {params}")
    clf = LGBMClassifier(**params)
    clf.fit(X, y)

    # 训练集内自检 (样本内, 非 OOS! 真 OOS 由 evolution.py 判)
    train_acc = float((clf.predict(X) == y).mean())
    # 诚实: 额外在 incumbent OOS 同口径 (match_date >= incumbent cutoff) 不做, 留 evolution 验证。

    meta = {
        "model": clf,
        "feat_cols": oe.INDEP_FEATS + oe.ODDS_OPEN_FEATS,
        "indep_feats": oe.INDEP_FEATS,
        "odds_feats": oe.ODDS_OPEN_FEATS,
        "version": "open-eye-candidate-v1",
        "trained_on": (f"matches.match_date < {args.cutoff}; "
                       f"特征=indep_features(预建, as-of kickoff) + odds_extra(odds_open) "
                       f"[复用 open_eye_predictor, parity=0]"),
        "train_n": int(len(X)),
        "oof_n": 0,
        "oof_metrics": {
            "train_acc": round(train_acc, 4),
            "note": "训练自检准确率(样本内, 仅证明拟合成功); 真 OOS 须 evolution.py --verify --oos-min-date>=cutoff",
        },
        "note": args.note or "开盘天眼候选: 仅开盘价+独立实力, 无前视. 待 evolution.py 五道关验证, 不自动晋升.",
    }
    os.makedirs(CANDIDATES_DIR, exist_ok=True)
    out = args.out or os.path.join(CANDIDATES_DIR,
                                    f"independent_model_open_eye.cand_{args.cutoff}.joblib")
    import joblib
    joblib.dump(meta, out)
    print(f"[save] candidate -> {out}")
    print(f"[save] trained_on={meta['trained_on']}")
    print(f"[save] train_n={meta['train_n']}  train_acc={train_acc:.4f} (样本内)")
    print(f"[next] evolution.py --verify {out} --oos-min-date {args.cutoff}   "
          f"(须 oos_min_date >= cutoff 才是真 OOS; 本脚本不晋升)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
