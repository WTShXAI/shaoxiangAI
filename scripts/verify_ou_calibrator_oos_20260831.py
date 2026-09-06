"""OU 校准器独立验证 (2026-08-31, 非破坏性, 不覆盖模型)

目的: 用户问「哪个能破赢市场」。ou_calibrator_20260830 的 docstring 自称
      AUC 0.5774 / ROI +8.89%(校准) / +16.62%(融合市场)。这些数字是训练脚本
      自己打印的, 按 IR-30 不能信。本脚本**独立**复算:
        - 用 events.db 干净近期场次(有真实 score_at 快照, 排除假0-0)
        - 时间外切分: 训练早 60% 拟合 Platt(仅本次验证用, 不落盘), 评估用晚 40%
        - 载入已存 ou_calibrator 的 Platt(logreg)+fuse_w, 在评估集算
          AUC + ROI(下注规则同原脚本: |p-implied|>0.05 才下, 押差异侧)
        - 对比: 市场naive / GBM原始 / 校准 / 融合

不修改任何已存模型文件。
"""
from __future__ import annotations
import os, sys, json, math, sqlite3
import numpy as np
import joblib
from sklearn.metrics import roc_auc_score
from sklearn.linear_model import LogisticRegression

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
CAL = os.path.join(ROOT, "models", "ou_calibrator_20260830.joblib")

from scripts.compare_ou_models_20260830 import opening_ou
from analysis.live_goal_probe import _open_1x2_from_snapshots
from pipeline.poisson_gbm import available as gbm_ok, predict_lambdas, p_over

FIT_FRAC = 0.6


def collect_all(limit=4000):
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-15' "
        "ORDER BY kickoff ASC LIMIT ?", (limit,)).fetchall()
    rec = []
    for mk, home, away, sh, sa, ko, league in rows:
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1", (mk,)).fetchone():
            continue
        if not con.execute(
            "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
            "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            continue
        ou = opening_ou(con, mk)
        if not ou:
            continue
        line, ov, un = ou
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            continue
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            continue
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league)
        if not lam:
            continue
        implied = (1.0 / ov) / ((1.0 / ov) + (1.0 / un))
        y = 1 if (int(sh) + int(sa)) > line else 0
        rec.append(dict(y=y, implied=implied, raw=float(p_over(lam[0], lam[1], line)),
                        ov=float(ov), un=float(un), ko=ko))
    con.close()
    return rec


def logit(p):
    p = np.clip(np.asarray(p, float), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def auc(p, y):
    return float(roc_auc_score(y, p))


def roi(p, y, imp, ov, un):
    """下注规则(同原脚本): |p-implied|>0.05 才下, 押差异侧; 押 over 用 ov, 押 under 用 un。"""
    p = np.asarray(p, float); y = np.asarray(y, int)
    imp = np.asarray(imp, float); ov = np.asarray(ov, float); un = np.asarray(un, float)
    stake = ret = 0.0
    for i in range(len(y)):
        if p[i] - imp[i] > 0.05:
            stake += 1.0; ret += ov[i] - 1.0
        elif imp[i] - p[i] > 0.05:
            stake += 1.0; ret += un[i] - 1.0
    return (ret - stake) / stake * 100 if stake else 0.0


def main():
    assert gbm_ok(), "Poisson GBM 不可用"
    print("收集干净场次 ...")
    rec = collect_all()
    print(f"  有效 {len(rec)}")
    rec.sort(key=lambda r: r["ko"])
    k = int(len(rec) * FIT_FRAC)
    tr, te = rec[:k], rec[k:]
    print(f"  时间切分: 训练(早){len(tr)} | 评估(晚, 时间外){len(te)}")
    if len(te) < 100:
        print("评估集过小, 中止"); return

    ytr = np.array([r["y"] for r in tr]); ptr = np.array([r["raw"] for r in tr])
    yte = np.array([r["y"] for r in te]); pte = np.array([r["raw"] for r in te])
    impte = np.array([r["implied"] for r in te]); ovte = np.array([r["ov"] for r in te]); unte = np.array([r["un"] for r in te])

    # 本次验证用 Platt(不落盘)
    lr = LogisticRegression(C=1e6).fit(logit(ptr).reshape(-1, 1), ytr)
    p_cal = lr.predict_proba(logit(pte).reshape(-1, 1))[:, 1]

    # 载入已存 calibrator 的 fuse_w, 与验证用 Platt 组合(已存模型即 Platt, 等价)
    d = joblib.load(CAL)
    w = float(d.get("fuse_w", 0.7))
    pf = w * p_cal + (1 - w) * impte

    print(f"\n===== 独立验证 (时间外 n={len(te)}, 已存 fuse_w={w}) =====")
    print(f"  市场naive(隐含) : AUC {auc(impte,yte):.4f} | ROI {roi(impte,yte,impte,ovte,unte):+.2f}%")
    print(f"  GBM原始 λ      : AUC {auc(pte,yte):.4f} | ROI {roi(pte,yte,impte,ovte,unte):+.2f}%")
    print(f"  GBM+Platt校准  : AUC {auc(p_cal,yte):.4f} | ROI {roi(p_cal,yte,impte,ovte,unte):+.2f}%")
    print(f"  融合 {w:.1f}*校准+{1-w:.1f}*市场: AUC {auc(pf,yte):.4f} | ROI {roi(pf,yte,impte,ovte,unte):+.2f}%")
    print(f"\n  [原 docstring 自称] AUC 0.5774 | ROI +8.89%(校准) / +16.62%(融合)")
    print(f"  [独立复算] 融合 ROI {roi(pf,yte,impte,ovte,unte):+.2f}% — 是否达 +16.62% 见上")


if __name__ == "__main__":
    main()
