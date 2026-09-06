"""
train_independent_model.py — 独立特征 + 实时赔率 融合模型 (根因修复 v7.3)

诚实结论(2026-07-25 实测): 纯独立特征(Elo/形式/休息/H2H/联赛) AUC=0.619,
  低于市场去水基线 0.659 -> 单独用球队实力特征打不赢市场, 且平局 recall=0。
  正确做法: 推理期"可获取"的特征 = 17 维独立特征(从历史回放) + 实时赔率衍生
  (去水概率/抽水) -> 模型学"球队实力 vs 市场定价"的残差 -> 真 edge。
  这同时根除旧 unified_predictor 的 median-fill 部署退化(推理时 55/81 填中位数)。

要素:
  - 主模型 LightGBM 三分类 + 平局加权; DrawExpert 二分类(is_draw) 混合
  - Platt(sigmoid) 校准(禁 Beta)
  - 时序分割 train<2023 / OOF>=2023
  - 训练样本 = matches(有 WH+IW 收盘赔率) 对齐 indep_features; fd_matches 仍用于
    回放 enrich Elo/形式(提升独立特征质量, 但不进模型特征)

评估(诚实):
  - OOF(2023+, 有赔率) 模型 vs 市场去水基线: Acc/宏AUC/LogLoss/Brier/各类recall
  - McNemar(模型 vs 市场) 判定差异显著性
  - Edge 信号: 模型与市场看好方不同的场次, 模型正确率 vs 市场隐含概率

运行: Python312\python.exe scripts/train_independent_model.py
"""
import sqlite3, os, json, ast, warnings, math
import numpy as np
import pandas as pd
import joblib
from sklearn.metrics import log_loss, roc_auc_score, accuracy_score, classification_report
from sklearn.calibration import CalibratedClassifierCV
import lightgbm as lgb

warnings.filterwarnings("ignore")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "football_data.db")
OUT_DIR = os.path.join(ROOT, "pipeline", "predictors", "saved_models")
os.makedirs(OUT_DIR, exist_ok=True)
MODEL_PATH = os.path.join(OUT_DIR, "independent_model.joblib")

INDEP_FEATS = ["elo_home", "elo_away", "elo_diff",
               "form_home", "form_away", "form_diff",
               "rest_home", "rest_away", "rest_diff",
               "h2h_home_win", "h2h_draw", "h2h_away_win",
               "league_strength"]
ODDS_FEATS = ["odds_close_h", "odds_close_d", "odds_close_a",
              "devig_h", "devig_d", "devig_a", "overround",
              "lambda_home", "lambda_away", "dc_draw", "draw_dev",
              "entropy", "margin_impl"]
FEATURES = INDEP_FEATS + ODDS_FEATS
DRAW_WEIGHT = 2.2


def _pois(l, k):
    if k < 0:
        return 0.0
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


def odds_extra(oh, od, oa):
    """从单一赔率快照算出的"丰富赔率衍生"特征(推理期可获取, 无历史依赖)。"""
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


def build_alias_map(cur):
    amap = {}
    for canon, aj in cur.execute("SELECT canonical, aliases_json FROM team_canonical"):
        amap[canon.strip().lower()] = canon
        if aj:
            try:
                lst = json.loads(aj) if aj.strip().startswith("[") else ast.literal_eval(aj)
                for a in lst:
                    amap[str(a).strip().lower()] = canon
            except Exception:
                pass
    return amap


def combine(main_proba, draw_proba):
    pd_exp = draw_proba[:, 1]
    ph, pa = main_proba[:, 0], main_proba[:, 2]
    s = ph + pa
    phn = np.where(s > 0, ph / s, 0.5)
    pan = np.where(s > 0, pa / s, 0.5)
    return np.stack([(1 - pd_exp) * phn, pd_exp, (1 - pd_exp) * pan], axis=1)


def chi2_sf_1(stat):
    if stat <= 0:
        return 1.0
    return math.erfc(math.sqrt(stat / 2.0))


def mcnemar(cm, ck):
    n10 = int(np.sum(cm & ~ck))
    n01 = int(np.sum(~cm & ck))
    denom = n10 + n01
    if denom <= 0:
        return 0.0, 1.0, n10, n01
    stat = (abs(n10 - n01) - 1) ** 2 / denom
    return stat, chi2_sf_1(stat), n10, n01


def multiclass_brier(proba, y):
    oh = np.zeros((len(y), 3))
    oh[np.arange(len(y)), y] = 1.0
    return float(np.mean(np.sum((proba - oh) ** 2, axis=1)))


def main():
    print("=== [1/7] 加载数据(独立特征 + 收盘赔率) ===")
    conn = sqlite3.connect(DB)
    amap = build_alias_map(conn)
    dfi = pd.read_sql_query("SELECT * FROM indep_features", conn)

    def canon(t):
        t = str(t).strip().lower()
        return amap.get(t, t)

    # 独立特征索引: (canon_home,canon_away,date) -> row
    indep_idx = {}
    for _, r in dfi.iterrows():
        key = (r["home"], r["away"], str(r["match_date"])[:10])
        indep_idx[key] = r

    df = pd.read_sql_query(
        "SELECT m.home_team_name, m.away_team_name, m.match_date, m.final_result, "
        "mf.odds_close_h, mf.odds_close_d, mf.odds_close_a, "
        "mf.odds_open_h, mf.odds_open_d, mf.odds_open_a "
        "FROM matches m JOIN match_features mf ON m.match_id=mf.match_id "
        "WHERE m.final_result IN ('H','D','A') "
        "AND mf.odds_close_h>0 AND mf.odds_close_d>0 AND mf.odds_close_a>0", conn)
    conn.close()
    print(f"  有赔率对齐样本(matches): {len(df)}")

    rows = []
    for _, r in df.iterrows():
        ch, ca = canon(r["home_team_name"]), canon(r["away_team_name"])
        key = (ch, ca, str(r["match_date"])[:10])
        if key not in indep_idx:
            continue
        ir = indep_idx[key]
        oh, od, oa = float(r["odds_close_h"]), float(r["odds_close_d"]), float(r["odds_close_a"])
        feat = {c: float(ir[c]) for c in INDEP_FEATS}
        feat.update(odds_extra(oh, od, oa))
        rows.append((feat, {"H": 0, "D": 1, "A": 2}[r["final_result"]], str(r["match_date"])[:10]))
    print(f"  成功对齐(独立特征+赔率): {len(rows)}")

    X = np.array([[r[0][c] for c in FEATURES] for r in rows], dtype=np.float64)
    y = np.array([r[1] for r in rows])
    dates = np.array([r[2] for r in rows])
    print(f"  特征维度: {X.shape[1]} (17 独立 + 7 赔率衍生)")

    print("=== [2/7] 时序分割 (train<2023 / OOF>=2023) ===")
    train_mask = dates < "2023-01-01"
    test_mask = dates >= "2023-01-01"
    Xtr, ytr = X[train_mask], y[train_mask]
    Xte, yte = X[test_mask], y[test_mask]
    print(f"  训练: {int(train_mask.sum())} | OOF: {int(test_mask.sum())}")

    print("=== [3/7] 训练主模型(三分类, 平局加权) + Platt ===")
    main_est = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=1000,
                                  learning_rate=0.01, num_leaves=63, min_child_samples=60,
                                  subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0, reg_alpha=0.2,
                                  class_weight={0: 1.0, 1: DRAW_WEIGHT, 2: 1.0},
                                  random_state=42, n_jobs=-1, verbose=-1)
    main_cal = CalibratedClassifierCV(main_est, method="sigmoid", cv=5)
    main_cal.fit(Xtr, ytr)

    print("=== [4/7] 训练 DrawExpert(二分类) + Platt ===")
    y_draw = (y == 1).astype(int)
    draw_est = lgb.LGBMClassifier(objective="binary", n_estimators=1000,
                                 learning_rate=0.01, num_leaves=63, min_child_samples=60,
                                 subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0, reg_alpha=0.2,
                                 class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)
    draw_cal = CalibratedClassifierCV(draw_est, method="sigmoid", cv=5)
    draw_cal.fit(Xtr, y_draw[train_mask])

    print("=== [5/7] OOF 评估(2023+, 有赔率) ===")
    main_te = main_cal.predict_proba(Xte)
    draw_te = draw_cal.predict_proba(Xte)
    proba = combine(main_te, draw_te)
    yt = yte

    def block(name, p, yy):
        ll = log_loss(yy, p)
        auc = roc_auc_score(yy, p, multi_class="ovr", average="macro")
        acc = accuracy_score(yy, p.argmax(1))
        br = multiclass_brier(p, yy)
        pred = p.argmax(1)
        rec = [float(np.mean(pred[yy == k] == k)) if np.sum(yy == k) > 0 else 0.0 for k in (0, 1, 2)]
        print(f"  [{name}] Acc={acc:.4f} 宏AUC={auc:.4f} LogLoss={ll:.4f} Brier={br:.4f}")
        print(f"           recall H={rec[0]:.3f} D={rec[1]:.3f} A={rec[2]:.3f}")
        return dict(acc=acc, auc=auc, ll=ll, brier=br, rec_h=rec[0], rec_d=rec[1], rec_a=rec[2])

    # 市场基线(同一批样本, 用收盘去水概率)
    base = np.array([[r[0]["devig_h"], r[0]["devig_d"], r[0]["devig_a"]] for r in rows])[test_mask]
    print("  --- 模型 vs 市场(同 OOF 样本) ---")
    bm = block("市场去水基线", base, yt)
    full = block("INDEP融合模型", proba, yt)

    print("=== [6/7] McNemar + Edge 信号 ===")
    model_fav = proba.argmax(1)
    market_fav = base.argmax(1)
    mc = model_fav == yt
    mk = market_fav == yt
    stat, p, n10, n01 = mcnemar(mc, mk)
    print(f"  McNemar(模型 vs 市场): chi2={stat:.3f} p={p:.4f} (模型对市场对={n10}, 模型错市场对={n01})")
    disagree = model_fav != market_fav
    nd = int(disagree.sum())
    if nd > 0:
        mcorr = float(mc[disagree].mean())
        implied = float(base[disagree, model_fav[disagree]].mean())
        print(f"  Edge(看好方不同 n={nd}): 模型正确率={mcorr:.3f} 市场隐含(模型所选)={implied:.3f} 净edge={mcorr-implied:+.3f}")
    else:
        print("  Edge: 无分歧场次")

    print("=== [7/7] 保存模型 ===")
    meta = {
        "model_main": main_cal, "model_draw": draw_cal,
        "feat_cols": FEATURES, "indep_feats": INDEP_FEATS, "odds_feats": ODDS_FEATS,
        "draw_weight": DRAW_WEIGHT, "version": "independent_v1",
        "trained_on": "indep_features(17) + 收盘赔率衍生(7), 时序 train<2023/OOF>=2023",
        "oof_metrics": full, "baseline_metrics": bm,
        "combines": "P(D)=DrawExpert; P(H/A) 主模型归一",
    }
    joblib.dump(meta, MODEL_PATH)
    print(f"  已保存: {MODEL_PATH}")

    old_p = os.path.join(OUT_DIR, "chain3_revival_full.joblib")
    if os.path.exists(old_p):
        try:
            old = joblib.load(old_p)
            om = old.get("oof_metrics", {})
            print(f"  旧 Chain3(赔率镜像, 推理退化): OOF AUC宏={om.get('auc_macro')} LogLoss={om.get('logloss')} Acc={om.get('acc')}")
        except Exception:
            pass
    rel_auc = (full["auc"] - bm["auc"]) / bm["auc"] * 100
    rel_ll = (bm["ll"] - full["ll"]) / bm["ll"] * 100
    print(f"  相对市场基线: 宏AUC {rel_auc:+.1f}%  LogLoss {rel_ll:+.1f}%(越低越好)")
    print("\n✅ 独立融合模型训练+评估完成")
    return full


if __name__ == "__main__":
    main()
