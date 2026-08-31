# -*- coding: utf-8 -*-
"""
scripts.train_cs_odds_model — 用时间线 CS 赔率训练 26 类波胆模型 (2026-08-31)

数据: data/cs_odds_dataset.csv (scripts/build_cs_dataset.py 产出)
  3213 场 = events.db odds_snapshots 赛前快照 CS 赔率 + 完场干净比分
  IR-04 假0-0已过滤 (score_missing=1 剔除); 仅赛前快照 (CS 铁律)
  时间外切分: train 2570 (前80%) / test 643 (后20%, kickoff >= 2026-08-29T07:00)

模型:
  L0 市场基线: 市场 CS 去水概率 argmax / top-5 / top-10 (诚实基线, 不训)
  L1 LR      : sklearn LogisticRegression multinomial (26类, 简单基线)
  L2 XGB     : XGBClassifier multi:softprob, train 内部再切时序验证集早停 (防过拟合)

验证 (测试集 643 场, 时间外, 禁随机切分):
  top-1/top-5/top-10 命中率 / LogLoss / 多分类 Brier / 0:0 灵敏度
  三方向聚合 (主/平/客) vs 1X2 市场 — 看 CS 修正是否带来方向信息
  正偏离档检验: 模型概率 > 市场概率 的档位命中率 (识别"被低估比分"的能力)

诚实边界 (IR-20 分析非预测 / IR-30 宁PASS不伪造):
  26 类 top-1 极限很低 (市场基线 ~9%), 模型须显著超过市场才有价值
  测试仅 643 场, +2pp 级差异统计不显著 — 结论须诚实标注
  模型未注册 M1-M7 白名单 (MAX_MODELS=7 已满), 先作独立交付物, 接入需用户拍板

输出: models/cs_odds_xgb.joblib / models/cs_odds_lr.joblib / models/cs_odds_report.json|txt
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV = os.path.join(ROOT, "data", "cs_odds_dataset.csv")
META = os.path.join(ROOT, "data", "cs_odds_dataset_meta.json")
OUT_XGB = os.path.join(ROOT, "models", "cs_odds_xgb.joblib")
OUT_LR = os.path.join(ROOT, "models", "cs_odds_lr.joblib")
OUT_JSON = os.path.join(ROOT, "models", "cs_odds_report.json")
OUT_TXT = os.path.join(ROOT, "models", "cs_odds_report.txt")

SCORELINES = [f"{h}:{a}" for h in range(5) for a in range(5)]  # 0:0..4:4 (25)
OTHER_IDX = 25
N_CLASSES = 26

FEATURE_COLS = (
    [f"cs_p_{s}" for s in SCORELINES]
    + ["cs_p_其他", "cs_cheapest_p", "cs_overround"]
    + ["h2h_h", "h2h_d", "h2h_a", "ou_line", "ou_over_devig", "ah_line", "ah_home_devig"]
    + ["lg_avg_goals", "lg_home_win", "lg_draw"]
)


# ── 指标 ──────────────────────────────────────────────────────────────────
def topk_acc(probs: np.ndarray, y: np.ndarray, k: int) -> float:
    top = np.argsort(-probs, axis=1)[:, :k]
    return float(np.mean([y[i] in top[i] for i in range(len(y))]))


def multiclass_brier(probs: np.ndarray, y: np.ndarray) -> float:
    y_onehot = np.zeros_like(probs)
    y_onehot[np.arange(len(y)), y] = 1.0
    return float(np.mean((probs - y_onehot) ** 2))


def three_way(probs: np.ndarray) -> np.ndarray:
    """26类概率 → 主胜/平/客胜 三方向."""
    idx = np.arange(25).reshape(5, 5)
    hw = idx[np.tril_indices(5, -1)]   # 行>列 → 主胜
    aw = idx[np.triu_indices(5, 1)]    # 列>行 → 客胜
    dr = np.diag(idx)                  # 对角线 → 平
    out = np.zeros((probs.shape[0], 3))
    out[:, 0] = probs[:, hw].sum(axis=1)
    out[:, 1] = probs[:, dr].sum(axis=1)
    out[:, 2] = probs[:, aw].sum(axis=1)
    # "其他"档(25)无方向信息 → 等分到三方向 (中性处理, 不引入任何偏向)
    other = probs[:, OTHER_IDX]
    out[:, 0] += other / 3.0
    out[:, 1] += other / 3.0
    out[:, 2] += other / 3.0
    return out


def evaluate(name: str, probs: np.ndarray, y: np.ndarray,
             mkt_1x2: np.ndarray | None = None) -> dict:
    """probs: (n,26) 模型概率; y: 标签; mkt_1x2: (n,3) 市场1X2去水概率(可空)."""
    n = len(y)
    res = {
        "model": name,
        "n": int(n),
        "top1": topk_acc(probs, y, 1),
        "top3": topk_acc(probs, y, 3),
        "top5": topk_acc(probs, y, 5),
        "top10": topk_acc(probs, y, 10),
        "logloss": float(log_loss(y, probs, labels=list(range(N_CLASSES)))),
        "brier": multiclass_brier(probs, y),
    }
    # 0:0 灵敏度 (最大类)
    mask00 = y == 0
    if mask00.sum() > 0:
        top1 = np.argmax(probs, axis=1)
        res["sens_00"] = float((top1[mask00] == 0).mean())
        res["n_00"] = int(mask00.sum())
    # 三方向 vs 1X2 市场
    tw = three_way(probs)
    # 三方向真实标签: 主胜0/平1/客胜2 ("其他"档无方向 → 排除)
    hw_set = set()
    dr_set = set()
    aw_set = set()
    for h in range(5):
        for a in range(5):
            i = h * 5 + a
            if h > a:
                hw_set.add(i)
            elif h == a:
                dr_set.add(i)
            else:
                aw_set.add(i)
    y3 = np.array([0 if y[i] in hw_set else (1 if y[i] in dr_set else (2 if y[i] in aw_set else -1)) for i in range(n)])
    # "其他"档 (25) 无方向 → 排除
    mask_dir = y3 >= 0
    y3 = y3[mask_dir]
    tw = tw[mask_dir]
    if len(y3) > 0:
        res["dir3_acc"] = float((np.argmax(tw, axis=1) == y3).mean())
        res["n_dir"] = int(len(y3))
        if mkt_1x2 is not None:
            m3 = mkt_1x2[mask_dir]
            res["mkt_1x2_acc"] = float((np.argmax(m3, axis=1) == y3).mean())
    return res


# ── 正偏离档检验: 模型认为市场"最被低估"的档位, 实际命中率 vs 市场概率 ──
def positive_deviation_check(probs: np.ndarray, mkt_p: np.ndarray, y: np.ndarray,
                             odds_map: np.ndarray | None = None) -> dict:
    """probs/mkt_p: (n,26) 模型/市场概率; odds_map: (n,26) 市场原始赔率(可空)."""
    diff = probs - mkt_p
    best = np.argmax(diff, axis=1)          # 每场模型偏离市场最大的档
    hit = best == y
    p_model = probs[np.arange(len(y)), best]
    p_mkt = mkt_p[np.arange(len(y)), best]
    out = {
        "n": int(len(y)),
        "hit_rate": float(hit.mean()),
        "avg_model_p": float(p_model.mean()),
        "avg_mkt_p": float(p_mkt.mean()),
        "avg_diff": float(diff.max(axis=1).mean()),
    }
    if odds_map is not None:
        odds = odds_map[np.arange(len(y)), best]
        ret = np.where(hit, odds - 1.0, -1.0)
        out["roi_level"] = float(ret.mean())          # 等额下注 ROI (参考, 非实盘依据)
        out["n_win"] = int(hit.sum())
    return out


def main() -> None:
    t0 = time.time()
    df = pd.read_csv(CSV, encoding="utf-8-sig")
    meta = json.load(open(META, encoding="utf-8"))
    print(f"加载 {len(df)} 场 (train {int((df['split']=='train').sum())} / test {int((df['split']=='test').sum())})")

    # 缺失填充: ou/ah 少量缺失; lg_* 410 场缺失(联赛无历史) → 中位数
    n_fill = 0
    for col in FEATURE_COLS:
        miss = int(df[col].isnull().sum())
        if miss:
            med = df[col].median()
            df[col] = df[col].fillna(med)
            n_fill += miss
            print(f"  填充 {col}: {miss} 行, 中位数={med:.4f}")
    print(f"  共填充 {n_fill} 个缺失值")

    X = df[FEATURE_COLS].to_numpy(dtype=np.float64)
    y = df["label"].to_numpy(dtype=np.int64)
    mkt_p = df[[f"cs_p_{s}" for s in SCORELINES] + ["cs_p_其他"]].to_numpy(dtype=np.float64)
    mkt_1x2 = df[["h2h_h", "h2h_d", "h2h_a"]].to_numpy(dtype=np.float64)
    odds_map = None  # 原始赔率未入CSV(存的是去水概率); ROI 用 1/去水后概率近似 → 禁, 诚实披露

    is_test = (df["split"] == "test").to_numpy()
    X_tr, y_tr = X[~is_test], y[~is_test]
    X_te, y_te = X[is_test], y[is_test]
    mkt_te, mkt_1x2_te = mkt_p[is_test], mkt_1x2[is_test]
    print(f"训练 {len(X_tr)} / 测试 {len(X_te)}")

    # train 内部按时间再切 15% 验证 (早停用, 不动 test)
    ko_tr = df.loc[~is_test, "kickoff_ts"].to_numpy()
    order = np.argsort(ko_tr)
    X_tr, y_tr, ko_tr = X_tr[order], y_tr[order], ko_tr[order]
    n_val = max(1, int(len(X_tr) * 0.15))
    X_val, y_val = X_tr[-n_val:], y_tr[-n_val:]
    X_tr, y_tr = X_tr[:-n_val], y_tr[:-n_val]
    print(f"  内部验证集 {len(X_val)} (早停用)")

    results = []
    lines = []
    def say(s=""):
        print(s)
        lines.append(s)

    # ── L0 市场基线 (不训) ──
    say("=" * 72)
    say(f"[L0] 市场 CS 概率基线 (测试集 {len(X_te)} 场)")
    res_mkt = evaluate("market_cs", mkt_te, y_te, mkt_1x2_te)
    res_mkt["dir3_acc"] = res_mkt.get("mkt_1x2_acc")  # 市场方向即1X2
    results.append(res_mkt)
    say(f"  top1={res_mkt['top1']:.4f} top5={res_mkt['top5']:.4f} top10={res_mkt['top10']:.4f} "
        f"logloss={res_mkt['logloss']:.4f} brier={res_mkt['brier']:.4f}")

    # ── L1 逻辑回归 ──
    say("=" * 72)
    say("[L1] LogisticRegression multinomial (26类, max_iter=2000)")
    lr = LogisticRegression(max_iter=2000, C=0.5, multi_class="multinomial", solver="lbfgs")
    lr.fit(X_tr, y_tr)
    p_lr = lr.predict_proba(X_te)
    res_lr = evaluate("logistic_regression", p_lr, y_te, mkt_1x2_te)
    results.append(res_lr)
    say(f"  top1={res_lr['top1']:.4f} top5={res_lr['top5']:.4f} top10={res_lr['top10']:.4f} "
        f"logloss={res_lr['logloss']:.4f} brier={res_lr['brier']:.4f}")

    # ── L2 XGBoost (早停) ──
    try:
        import xgboost as xgb
        say("=" * 72)
        say("[L2] XGBoost multi:softprob (防过拟合参数, 早停)")
        xm = xgb.XGBClassifier(
            objective="multi:softprob", num_class=N_CLASSES,
            n_estimators=500, learning_rate=0.05, max_depth=3,
            min_child_weight=3, subsample=0.8, colsample_bytree=0.7,
            reg_lambda=2.0, eval_metric="mlogloss", n_jobs=4,
            early_stopping_rounds=30,
        )
        xm.fit(X_tr, y_tr, eval_set=[(X_val, y_val)], verbose=False)
        p_xgb = xm.predict_proba(X_te)
        res_xgb = evaluate("xgboost", p_xgb, y_te, mkt_1x2_te)
        res_xgb["best_iter"] = int(xm.best_iteration) if hasattr(xm, "best_iteration") else None
        results.append(res_xgb)
        say(f"  best_iter={res_xgb.get('best_iter')} "
            f"top1={res_xgb['top1']:.4f} top5={res_xgb['top5']:.4f} top10={res_xgb['top10']:.4f} "
            f"logloss={res_xgb['logloss']:.4f} brier={res_xgb['brier']:.4f}")
        xgb_ok = True
    except Exception as e:
        say(f"  [XGB 失败] {e}")
        xgb_ok = False

    # ── 三方向 vs 1X2 市场 ──
    say("=" * 72)
    say("[三方向聚合] 主胜/平/客胜 (排除'其他'档无方向场次)")
    for r in results:
        if "n_dir" in r and r["n_dir"]:
            say(f"  {r['model']:<18} dir3_acc={r.get('dir3_acc', float('nan')):.4f} (n={r['n_dir']})")
        elif r["model"] == "market_cs":
            say(f"  {'market_1x2':<18} dir3_acc={r.get('mkt_1x2_acc', float('nan')):.4f} (n={r.get('n_dir', 0)})")

    # ── 正偏离档检验 (模型 vs 市场) ──
    say("=" * 72)
    say("[正偏离档检验] 模型概率-市场概率最大的档位, 实际命中率 (识别被低估比分)")
    dev_models = []
    for r in results:
        if r["model"] == "market_cs":
            continue
        if r["model"] == "xgboost" and not xgb_ok:
            continue
        dev_models.append(r["model"])
    for name in dev_models:
        p = p_xgb if name == "xgboost" else p_lr
        d = positive_deviation_check(p, mkt_te, y_te)
        r = next(x for x in results if x["model"] == name)
        r["deviation_check"] = d
        say(f"  {name:<18} hit={d['hit_rate']:.4f} 模型p={d['avg_model_p']:.4f} "
            f"市场p={d['avg_mkt_p']:.4f} Δ={d['avg_diff']:.4f} (n={d['n']})")
        say(f"    解读: 模型高信念修正档命中 {d['hit_rate']*100:.1f}% vs 该档市场平均概率 {d['avg_mkt_p']*100:.1f}%")

    # ── 0:0 灵敏度 ──
    say("=" * 72)
    say("[0:0 灵敏度] (0:0 为最大类, 测试集占比最大)")
    for r in results:
        if "sens_00" in r:
            say(f"  {r['model']:<18} sens_00={r['sens_00']:.4f} (n_00={r['n_00']})")

    # ── 保存 ──
    say("=" * 72)
    models_saved = []
    try:
        from joblib import dump
        if xgb_ok:
            dump({"model": xm, "features": FEATURE_COLS, "classes": meta["classes"],
                  "kind": "xgboost"}, OUT_XGB)
            models_saved.append(os.path.basename(OUT_XGB))
            say(f"保存 {OUT_XGB}")
        dump({"model": lr, "features": FEATURE_COLS, "classes": meta["classes"],
              "kind": "logistic_regression"}, OUT_LR)
        models_saved.append(os.path.basename(OUT_LR))
        say(f"保存 {OUT_LR}")
    except Exception as e:
        say(f"  [保存失败] {e}")

    report = {
        "script": "scripts/train_cs_odds_model.py",
        "data": os.path.basename(CSV),
        "n_train": int(len(X_tr)),
        "n_val": int(n_val),
        "n_test": int(len(X_te)),
        "classes": meta["classes"],
        "features": FEATURE_COLS,
        "results": results,
        "saved_models": models_saved,
        "elapsed_s": round(time.time() - t0, 1),
        "disclaimer": "IR-20 分析非预测; 26类top1极限低; 643场测试样本小, ±2pp不显著; 未注册M1-M7白名单, 接入需拍板",
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    say(f"报告: {OUT_JSON}  (耗时 {report['elapsed_s']}s)")
    with open(OUT_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    say(f"文本报告: {OUT_TXT}")


if __name__ == "__main__":
    main()
