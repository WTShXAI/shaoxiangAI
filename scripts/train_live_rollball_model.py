# -*- coding: utf-8 -*-
"""
滚球 live 模型训练 (v1, 2026-08-24)
====================================
消费「滚球采集器」记录的 in-play 实时赔率快照:
  data/events.db.odds_snapshots  WHERE minute_at > 0  (329万行 / 1987场真实滚球)

输入特征 (给定滚球某一时刻的盘口状态):
  - minute_norm      : 比赛分钟 / 90
  - score_home/away  : 实时比分 (来自 score_at)
  - lead             : 主客净胜球
  - 1X2 去水概率      : p_h / p_d / p_a  (庄家实时胜平负预期, 去水)
  - OU 原始赔率(不去水): raw_over=1/over / raw_under=1/under / line
                        (专家判断: 滚球定价结构固定, 去水与否对方向模型等价;
                         A/B 实测 AUC 差 -0.0001 / logloss 差 +0.0003, 纯噪声)
  注: OU 与 1X2 各自独立采样 (OU 不再被 1X2 可用率门控, 否则欠训)。AH 本轮未纳入。

标签 (赛后真相, 无泄露):
  - 1X2 : match_outcomes.result (H/D/A -> 0/1/2)
  - OU  : 总球 > 该快照 OU 线 ? 1 : 0

防泄露: GroupKFold 按 match_key 分组 (同场多分钟不跨折)。

输出契约: data/live_{1x2,ou}_model.joblib (load + predict_proba)
"""
import sys, os, json, time, math, sqlite3
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import GroupKFold
from sklearn.metrics import roc_auc_score, log_loss, accuracy_score

# 单一特征真相源 (确保 train 与推理点特征严格一致)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis.live_rollball_features import (
    build_ou_features, build_1x2_features, FEAT_OU, FEAT_1X2)

GQ = r"D:\Architecture\data\events.db"
OUT = r"D:\Architecture\data"

def devig(*odds):
    vals = [o for o in odds if o and o > 1.0]
    if len(vals) < len(odds):
        return None
    inv = [1.0 / o for o in vals]
    s = sum(inv)
    if s <= 0:
        return None
    return [v / s for v in inv]

def parse_score(s):
    if not s:
        return (0, 0)
    s = str(s).strip()
    for sep in ["-", ":"]:
        if sep in s:
            try:
                a, b = s.split(sep)
                return (int(a), int(b))
            except Exception:
                continue
    return (0, 0)

def load_labels():
    c = sqlite3.connect(GQ, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    # in-play match_keys
    mks = [r[0] for r in c.execute(
        "SELECT DISTINCT match_key FROM odds_snapshots WHERE minute_at>0")]
    label = {}
    for mk in mks:
        if " vs " not in mk:
            continue
        h, a = mk.split(" vs ", 1)
        r = c.execute(
            "SELECT result, score_home, score_away FROM match_outcomes "
            "WHERE home=? AND away=? AND result IS NOT NULL", (h, a)).fetchone()
        if r:
            label[mk] = (r[0], r[1], r[2])
    c.close()
    return label

def load_rows():
    c = sqlite3.connect(GQ, timeout=60)
    c.execute("PRAGMA busy_timeout=30000")
    # 折叠: 每个 (match_key, minute, market, selection) 取最新(id最大) 一条
    q = """
    WITH last AS (
      SELECT match_key, minute_at, market, selection, MAX(id) AS maxid
      FROM odds_snapshots WHERE minute_at>0
      GROUP BY match_key, minute_at, market, selection
    )
    SELECT o.match_key, o.minute_at, o.market, o.selection, o.odds, o.line, o.score_at
    FROM odds_snapshots o JOIN last l ON o.id = l.maxid
    """
    df = pd.read_sql_query(q, c)
    c.close()
    return df

def pivot(df):
    rows = {}
    for _, r in df.iterrows():
        key = (r["match_key"], int(r["minute_at"]))
        d = rows.setdefault(key, {"match_key": r["match_key"], "minute": int(r["minute_at"]),
                                   "score_at": r["score_at"]})
        mkt = r["market"]; sel = r["selection"]; o = r["odds"]; ln = r["line"]
        if mkt == "1X2":
            if sel in ("home", "draw", "away"):
                d["1x2_" + sel] = o
        elif mkt.startswith("OU_"):
            if sel in ("over", "under") and ln is not None:
                ous = d.setdefault("ous", {})
                ous.setdefault(str(ln), {})[sel] = o
        elif mkt.startswith("AH_"):
            if sel in ("home", "away"):
                d["ah_" + sel] = o
                d["ah_line"] = ln
    return list(rows.values())

def build():
    """返回两个独立数据集: (1X2 数据集, OU 数据集)。
    OU 不再被 1X2 可用率门控 —— 有 OU over/under 即独立采样, 避免欠训。
    OU 特征用原始 1/odds (不去水); 1X2 特征用去水概率。
    """
    print("[1/4] load labels ...")
    labels = load_labels()
    print("    labeled in-play matches:", len(labels))
    print("[2/4] load + collapse in-play snapshots ...")
    t0 = time.time()
    df = load_rows()
    print("    collapsed rows:", len(df), "(%.1fs)" % (time.time() - t0))
    print("[3/4] pivot -> feature rows ...")
    recs = pivot(df)
    print("    pivoted (match,minute) rows:", len(recs))
    X1, y1, g1 = [], [], []   # 1X2 数据集 (需完整 1X2 live)
    Xo, yo, go = [], [], []   # OU 数据集  (需 OU over/under, 与 1X2 独立)
    for d in recs:
        mk = d["match_key"]
        if mk not in labels:
            continue
        res, sh, sa = labels[mk]
        sc = parse_score(d.get("score_at"))
        minute = max(1, min(95, d["minute"]))
        # --- 1X2 (去水, 维持原口径) ---
        # 2026-08-27 防御: match_outcomes.result 历史脏值(H/A/D 别名)已统一归一,
        # 但保留跳过未知值逻辑, 避免未来脏数据再阻断整轮复训。
        _res_map = {"home": 0, "draw": 1, "away": 2, "H": 0, "A": 2, "D": 1}
        if res not in _res_map:
            continue
        p1 = devig(d.get("1x2_home"), d.get("1x2_draw"), d.get("1x2_away"))
        if p1 is not None:
            X1.append(build_1x2_features(minute, sc[0], sc[1], p1[0], p1[1], p1[2]))
            y1.append(_res_map[res])
            g1.append(mk)
        # --- OU (结构性特征, 每条 OU 线独立成样本) ---
        for ln_str, entry in d.get("ous", {}).items():
            if "over" in entry and "under" in entry:
                try:
                    ou_line = float(ln_str)
                except Exception:
                    continue
                ou_over = entry["over"]; ou_under = entry["under"]
                if not (ou_over and ou_under and ou_over > 1.0 and ou_under > 1.0):
                    continue
                Xo.append(build_ou_features(minute, sc[0], sc[1], ou_line, ou_over, ou_under))
                yo.append(1 if (sh + sa) > ou_line else 0)
                go.append(mk)
    X1 = np.array(X1, dtype=float)
    Xo = np.array(Xo, dtype=float)
    print("    1X2 rows:", len(X1), "| OU rows:", len(Xo))
    return (X1, np.array(y1), np.array(g1)), (Xo, np.array(yo), np.array(go))

def cv_train(X, y, groups, task, feat_names):
    n_cls = 3 if task == "1x2" else 2
    cfg = dict(num_leaves=31, min_child_samples=40, subsample=0.8, colsample_bytree=0.8,
               reg_lambda=5.0, reg_alpha=0.5, learning_rate=0.02, n_estimators=1000,
               early_stopping_rounds=50, random_state=0, n_jobs=-1, verbose=-1)
    if task == "1x2":
        cfg["class_weight"] = "balanced"
    gkf = GroupKFold(n_splits=5)
    oof = np.zeros((len(y), n_cls))
    aucs, lls, accs = [], [], []
    for tr, te in gkf.split(X, y, groups):
        if task == "1x2":
            clf = lgb.LGBMClassifier(objective="multiclass", num_class=3, **cfg)
        else:
            clf = lgb.LGBMClassifier(objective="binary", **cfg)
        Xval, yval = X[tr[-max(2000, len(tr)//10):]], y[tr[-max(2000, len(tr)//10):]]
        Xtr, ytr = X[tr[:-max(2000, len(tr)//10)]], y[tr[:-max(2000, len(tr)//10)]]
        clf.fit(Xtr, ytr, eval_set=[(Xval, yval)],
                eval_metric="multi_logloss" if task == "1x2" else "binary_logloss")
        p = clf.predict_proba(X[te])
        oof[te] = p
        if task == "ou":
            aucs.append(roc_auc_score(y[te], p[:, 1]))
            lls.append(log_loss(y[te], p[:, 1]))
            accs.append(accuracy_score(y[te], p[:, 1] > 0.5))
        else:
            aucs.append(roc_auc_score(y[te], p, multi_class="ovr"))
            lls.append(log_loss(y[te], p))
            accs.append(accuracy_score(y[te], p.argmax(1)))
    # full refit (no early stopping -> train all n_estimators)
    full_cfg = {k: v for k, v in cfg.items() if k != "early_stopping_rounds"}
    if task == "1x2":
        full = lgb.LGBMClassifier(objective="multiclass", num_class=3, **full_cfg)
    else:
        full = lgb.LGBMClassifier(objective="binary", **full_cfg)
    full.fit(X, y)
    return full, {"auc": (np.mean(aucs), np.std(aucs)),
                  "ll": (np.mean(lls), np.std(lls)),
                  "acc": (np.mean(accs), np.std(accs))}

def main():
    (X1, y1, g1), (Xo, yo, go) = build()
    feat_1x2 = FEAT_1X2
    feat_ou = FEAT_OU
    # 1X2 (de-vig, unchanged)
    print("[4/4] train live 1X2 (GroupKFold by match) ...")
    m1, r1 = cv_train(X1, y1, g1, "1x2", feat_1x2)
    print("  1X2  AUC=%.4f±%.4f  logloss=%.4f±%.4f  acc=%.4f±%.4f" %
          (r1["auc"][0], r1["auc"][1], r1["ll"][0], r1["ll"][1], r1["acc"][0], r1["acc"][1]))
    # OU (RAW 1/odds, no de-vig; independent of 1X2)
    print("  train live OU on %d rows (RAW 1/odds, NO de-vig) ..." % len(Xo))
    m2, r2 = cv_train(Xo, yo, go, "ou", feat_ou)
    print("  OU   AUC=%.4f±%.4f  logloss=%.4f±%.4f  acc=%.4f±%.4f" %
          (r2["auc"][0], r2["auc"][1], r2["ll"][0], r2["ll"][1], r2["acc"][0], r2["acc"][1]))
    # save
    import joblib
    joblib.dump(m1, os.path.join(OUT, "live_1x2_model.joblib"))
    joblib.dump(m2, os.path.join(OUT, "live_ou_model.joblib"))
    report = {
        "built": "2026-08-24",
        "source": "events.db odds_snapshots (minute_at>0) + match_outcomes result",
        "ou_features": "结构性12维(含 total/goals_needed_over/minutes_remaining/exp_remaining_goals 泊松先验 + struct_p_over 泊松结构性先验 P(大)); raw 1/odds 不去水; 根治进球瞬间 whipsaw",
        "ou_independent_of_1x2": True,
        "inplay_matches": int(len(set(g1.tolist()) | set(go.tolist()))),
        "labeled_matches_1x2": int(len(set(g1.tolist()))),
        "labeled_matches_ou": int(len(set(go.tolist()))),
        "rows_1x2": int(len(X1)),
        "rows_ou": int(len(Xo)),
        "cv_1x2": {k: [float(v[0]), float(v[1])] for k, v in [(kk, r1[kk]) for kk in r1]},
        "cv_ou": {k: [float(v[0]), float(v[1])] for k, v in [(kk, r2[kk]) for kk in r2]},
    }
    with open(os.path.join(OUT, "live_rollball_cv_report.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("[SAVE] data/live_1x2_model.joblib, data/live_ou_model.joblib, data/live_rollball_cv_report.json")

if __name__ == "__main__":
    main()
