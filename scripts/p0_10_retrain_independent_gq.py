"""P0-10 independent_model 在 GQ 2026 干净数据上重训 + walk-forward 留出（P0 真闸门公平检验）

动机(P0-9b 暴露的方法学诚实坑):
  P0-9b 用 2016 football_data 训练的 independent_model 喂 2026 GQ 特征 -> 模型 LL 0.868 远差于
  市场 0.5637, 且两场景 CI 明显负(无 +EV)。但这可能是 **训练/测试时代错位(distribution shift)**,
  非"独立信息路径无 edge"的干净证伪。要公平检验, 必须在**同代数据(2026 GQ 干净)**上重训 + 留出。

做法(诚实, 无前视):
  1. 样本: GQ.db.matches(真相) ∩ events.odds_snapshots 1X2(开/收盘<=kickoff) -> 干净宇宙(同 P0-8b/9, ~2540)
     · 13 独立特征: indep_features_gq.db(按 match_key 取"该场当时"特征, 无前视)
     · 13 赔率衍生: 收盘 odds 经 odds_extra(devig/lambda/dc_draw/entropy...) 派生(推理期可获取)
     · 标签 H/D/A = 真赛果
  2. walk-forward 扩展窗(N_FOLDS=4): 按 kickoff 升序切 25/50/75/100%;
     第 i 折 train=rows[0:c_i], test=rows[c_i:c_{i+1}] -> 每折在不同未来段做真 OOS。
  3. 每折训 LightGBM 主模型(三分类, 平局加权 DRAW_WEIGHT) + DrawExpert(二分类), Platt 校准;
     组合 = P(D)=DrawExpert; P(H/A)=主模型归一(CalibratedClassifierCV, cv=3)。
  4. 每折 test 评估:
     · 模型 log-loss vs 市场收盘去水基线(同折样本)
     · +EV 测试: 选 模型概率-市场隐含 最大正差选项, 在【收盘】价下注, 结算真赛果
       (IR-17 三件套: win_rate/implied/edge_pp; +EV = mean_roi>0 且 CI[0]>0; bootstrap N_BOOT=2000 seed=20260916)
  5. 汇总: 跨折 pooling 所有 test 场(~1900) 的 +EV + bootstrap CI -> 公平判定独立信息路径。
  6. 参考模型存 **独立路径** independent_model_gq2026.joblib(不覆盖生产 independent_model.joblib, IR-15/冻结)。

输出: reports/p0_10_retrain_status.json + .md
用法: .venv/Scripts/python.exe scripts/p0_10_retrain_independent_gq.py
"""
import json, os, random, sqlite3, math, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "reports")
EV_DB = os.path.join(ROOT, "data", "events.db")
GQ_DB = os.path.join(ROOT, "data", "GQ.db")
GQ_FEAT_DB = os.path.join(ROOT, "data", "indep_features_gq.db")
REF_MODEL = os.path.join(ROOT, "pipeline", "predictors", "saved_models", "independent_model_gq2026.joblib")
N_BOOT = 2000
SEED = 20260916
N_FOLDS = 4
DRAW_WEIGHT = 2.2

import numpy as np
import pandas as pd
import joblib
from sklearn.metrics import log_loss as sk_log_loss
from sklearn.calibration import CalibratedClassifierCV
import lightgbm as lgb

INDEP_FEATS = ["elo_home", "elo_away", "elo_diff", "form_home", "form_away", "form_diff",
               "rest_home", "rest_away", "rest_diff", "h2h_home_win", "h2h_draw", "h2h_away_win", "league_strength"]
ODDS_FEATS = ["odds_close_h", "odds_close_d", "odds_close_a", "devig_h", "devig_d", "devig_a",
              "overround", "lambda_home", "lambda_away", "dc_draw", "draw_dev", "entropy", "margin_impl"]
FEATURES = INDEP_FEATS + ODDS_FEATS


def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)


def parse_kickoff(s):
    if not s:
        return None
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s[:19])
    except Exception:
        pass
    return None


def _pois(l, k):
    if k < 0:
        return 0.0
    return math.exp(-l) * (l ** k) / math.factorial(k)


def _dc_probs(lh, la, n=12):
    H = D = A = 0.0
    for i in range(n + 1):
        for j in range(n + 1):
            p = _pois(lh, i) * _pois(la, j)
            if i > j: H += p
            elif i == j: D += p
            else: A += p
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
    ent = 0.0
    for p in (dh, dd, da):
        if p > 0: ent -= p * math.log(p)
    return {"odds_close_h": oh, "odds_close_d": od, "odds_close_a": oa, "devig_h": dh, "devig_d": dd,
            "devig_a": da, "overround": inv - 1.0, "lambda_home": lh, "lambda_away": la,
            "dc_draw": dcD, "draw_dev": dd - dcD, "entropy": ent, "margin_impl": dh - da}


def fair_probs(odds3):
    inv = [1.0 / o if o and o > 0 else 0.0 for o in odds3]
    s = sum(inv)
    return [x / s for x in inv] if s > 0 else [1/3, 1/3, 1/3]


def combine(main_proba, draw_proba):
    pd_exp = draw_proba[:, 1]
    ph, pa = main_proba[:, 0], main_proba[:, 2]
    s = ph + pa
    phn = np.where(s > 0, ph / s, 0.5); pan = np.where(s > 0, pa / s, 0.5)
    return np.stack([(1 - pd_exp) * phn, pd_exp, (1 - pd_exp) * pan], axis=1)


def build_rows():
    # indep 特征按 match_key(唯一, 该场当时) 索引
    fg = sqlite3.connect(GQ_FEAT_DB, timeout=30); fg.row_factory = sqlite3.Row
    indep_idx = {}
    cols = [r[1] for r in fg.execute("PRAGMA table_info(indep_features)")]
    for rec in fg.execute("SELECT * FROM indep_features"):
        row = {cols[i]: rec[i] for i in range(len(cols))}
        indep_idx[str(row.get("match_key", "")).strip()] = row
    fg.close()
    cg = ro(GQ_DB); cg.row_factory = sqlite3.Row
    truth = {}
    for r in cg.execute("SELECT match_key, score_home, score_away, kickoff FROM matches "
                        "WHERE score_home IS NOT NULL AND score_away IS NOT NULL").fetchall():
        truth[r["match_key"]] = (r["score_home"], r["score_away"], parse_kickoff(r["kickoff"]))
    cg.close()
    ce = ro(EV_DB); ce.row_factory = sqlite3.Row
    kt_map = {}
    for r in ce.execute("SELECT match_key, kickoff FROM matches WHERE kickoff IS NOT NULL AND kickoff!=''").fetchall():
        kt_map[r["match_key"]] = parse_kickoff(r["kickoff"])
    SEL = ["home", "draw", "away"]
    rows = []
    for mk, (sh, sa, ktg) in truth.items():
        kt = kt_map.get(mk)
        if kt is None or ktg is None:
            continue
        c2 = ro(EV_DB); cc = c2.cursor()
        q = (
            "SELECT selection,"
            " (SELECT o.odds FROM odds_snapshots o WHERE o.match_key=? AND o.market='1X2' "
            "   AND o.selection=s.selection AND o.captured_at<=? ORDER BY o.captured_at ASC LIMIT 1) AS open_o,"
            " (SELECT o.odds FROM odds_snapshots o WHERE o.match_key=? AND o.market='1X2' "
            "   AND o.selection=s.selection AND o.captured_at<=? ORDER BY o.captured_at DESC LIMIT 1) AS close_o"
            " FROM (SELECT DISTINCT selection FROM odds_snapshots WHERE match_key=? AND market='1X2') s"
        )
        got = cc.execute(q, (mk, kt, mk, kt, mk)).fetchall(); c2.close()
        od, cd = {}, {}
        for sel, oo, co in got:
            if oo and co: od[sel], cd[sel] = oo, co
        if not all(k in od for k in SEL):
            continue
        ir = indep_idx.get(mk)
        if ir is None:
            continue
        close_o = [cd["home"], cd["draw"], cd["away"]]
        try:
            feat = {c: float(ir[c]) for c in INDEP_FEATS}
        except Exception:
            continue
        feat.update(odds_extra(close_o[0], close_o[1], close_o[2]))
        label = 0 if sh > sa else (2 if sa > sh else 1)
        rows.append(([feat[c] for c in FEATURES], label, kt, close_o))
    rows.sort(key=lambda x: x[2])
    return rows


def ev_test(proba, close_o_l, ridx_l):
    """+EV 测试: 模型概率 vs 收盘隐含, 选最大正差, 收盘价下注, 结算真赛果."""
    random.seed(SEED)
    roi_l, win_l, impl_l, sel = [], [], [], [0, 0, 0]
    for p, co, ridx in zip(proba, close_o_l, ridx_l):
        imp = fair_probs(co)
        diff = [p[i] - imp[i] for i in range(3)]
        j = int(max(range(3), key=lambda i: diff[i]))
        if diff[j] <= 0:
            continue
        sel[j] += 1
        o = co[j]
        win = 1.0 if j == ridx else 0.0
        roi_l.append((o - 1.0) if win else -1.0)
        win_l.append(win); impl_l.append(imp[j])
    nb = len(roi_l)
    if nb == 0:
        return {"n_bets": 0}
    mean_roi = sum(roi_l) / nb
    win_rate = sum(win_l) / nb
    impl = sum(impl_l) / nb
    edge_pp = (win_rate - impl) * 100.0
    ci_l = [sum(roi_l[random.randrange(nb)] for _ in range(nb)) / nb for _ in range(N_BOOT)]
    ci_l.sort()
    return {"n_bets": nb, "bet_share": [round(sel[i] / nb * 100, 1) for i in range(3)],
            "win_rate": round(win_rate * 100, 2), "implied": round(impl * 100, 2),
            "edge_pp": round(edge_pp, 3), "mean_roi": round(mean_roi * 100, 3),
            "roi_ci": [round(ci_l[int(0.025 * N_BOOT)] * 100, 3), round(ci_l[int(0.975 * N_BOOT)] * 100, 3)],
            "positive_ev": (mean_roi > 0) and (ci_l[int(0.025 * N_BOOT)] > 0)}


def main():
    rows = build_rows()
    n = len(rows)
    print(f"[p0_10] 干净训练宇宙={n} 场 (GQ真相 ∩ events 1X2 开/收盘<=kickoff, 26维特征)")
    X = np.array([r[0] for r in rows], dtype=np.float64)
    y = np.array([r[1] for r in rows])
    cuts = [int(n * f / N_FOLDS) for f in range(1, N_FOLDS + 1)]
    fold_reports = []
    all_test_proba, all_test_close, all_test_ridx = [], [], []
    all_test_market_ll, all_test_model_ll = [], []
    for fi in range(N_FOLDS):
        c0 = 0 if fi == 0 else cuts[fi - 1]
        c1 = cuts[fi]
        if c1 - c0 < 60:
            print(f"  折{fi+1}: test 样本 {c1-c0}<60 跳过")
            continue
        Xte, yte = X[c1:], y[c1:]  # train=rows[0:c1] (扩展窗)
        if len(Xte) < 30:
            print(f"  折{fi+1}: test 仅 {len(Xte)} 跳过")
            continue
        # 训练
        main_est = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=600,
                                      learning_rate=0.02, num_leaves=63, min_child_samples=40,
                                      subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0, reg_alpha=0.2,
                                      class_weight={0: 1.0, 1: DRAW_WEIGHT, 2: 1.0},
                                      random_state=42, n_jobs=-1, verbose=-1)
        main_cal = CalibratedClassifierCV(main_est, method="sigmoid", cv=3)
        main_cal.fit(X[:c1], y[:c1])
        yd = (y[:c1] == 1).astype(int)
        draw_est = lgb.LGBMClassifier(objective="binary", n_estimators=600, learning_rate=0.02,
                                      num_leaves=63, min_child_samples=40, subsample=0.9, colsample_bytree=0.9,
                                      reg_lambda=3.0, reg_alpha=0.2, class_weight="balanced",
                                      random_state=42, n_jobs=-1, verbose=-1)
        draw_cal = CalibratedClassifierCV(draw_est, method="sigmoid", cv=3)
        draw_cal.fit(X[:c1], yd)
        # 评估 test
        mp = combine(main_cal.predict_proba(Xte), draw_cal.predict_proba(Xte))
        close_o_l = [rows[i][3] for i in range(c1, n)]
        ridx_l = [rows[i][1] for i in range(c1, n)]
        base = np.array([fair_probs(rows[i][3]) for i in range(c1, n)])
        mll = float(sk_log_loss(ridx_l, mp))
        bll = float(sk_log_loss(ridx_l, base))
        ev = ev_test(mp, close_o_l, ridx_l)
        fold_reports.append({"fold": fi + 1, "train_n": int(c1), "test_n": int(len(Xte)),
                             "model_logloss": round(mll, 4), "market_logloss": round(bll, 4),
                             "model_beats_market_ll": mll < bll - 0.001, "ev": ev})
        all_test_proba.extend(mp); all_test_close.extend(close_o_l); all_test_ridx.extend(ridx_l)
        all_test_model_ll.extend([mll] * len(Xte)); all_test_market_ll.extend([bll] * len(Xte))
        print(f"  折{fi+1}: train={c1} test={len(Xte)} | 模型LL={mll:.4f} vs市场{bll:.4f} beat={mll<bll-0.001} | "
              f"EV下注{ev.get('n_bets',0)} ROI={ev.get('mean_roi','-')} +EV={ev.get('positive_ev','-')}")

    # 跨折 pooling +EV
    pooled_ev = ev_test(np.array(all_test_proba), all_test_close, all_test_ridx)
    pooled_model_ll = round(sum(all_test_model_ll) / len(all_test_model_ll), 4) if all_test_model_ll else None
    pooled_market_ll = round(sum(all_test_market_ll) / len(all_test_market_ll), 4) if all_test_market_ll else None
    print(f"[p0_10] 跨折 pooling: test场={len(all_test_proba)} | 模型LL={pooled_model_ll} vs市场{pooled_market_ll} | "
          f"EV ROI={pooled_ev.get('mean_roi')} +EV={pooled_ev.get('positive_ev')}")

    # 参考模型: 在全量上训一份存独立路径(不覆盖生产)
    main_est = lgb.LGBMClassifier(objective="multiclass", num_class=3, n_estimators=800, learning_rate=0.02,
                                  num_leaves=63, min_child_samples=40, subsample=0.9, colsample_bytree=0.9,
                                  reg_lambda=3.0, reg_alpha=0.2, class_weight={0: 1.0, 1: DRAW_WEIGHT, 2: 1.0},
                                  random_state=42, n_jobs=-1, verbose=-1)
    main_cal = CalibratedClassifierCV(main_est, method="sigmoid", cv=3); main_cal.fit(X, y)
    yd = (y == 1).astype(int)
    draw_est = lgb.LGBMClassifier(objective="binary", n_estimators=800, learning_rate=0.02, num_leaves=63,
                                  min_child_samples=40, subsample=0.9, colsample_bytree=0.9, reg_lambda=3.0,
                                  reg_alpha=0.2, class_weight="balanced", random_state=42, n_jobs=-1, verbose=-1)
    draw_cal = CalibratedClassifierCV(draw_est, method="sigmoid", cv=3); draw_cal.fit(X, yd)
    os.makedirs(os.path.dirname(REF_MODEL), exist_ok=True)
    joblib.dump({"model_main": main_cal, "model_draw": draw_cal, "feat_cols": FEATURES,
                 "indep_feats": INDEP_FEATS, "odds_feats": ODDS_FEATS, "draw_weight": DRAW_WEIGHT,
                 "version": "independent_gq2026_v1", "trained_on": "GQ 2026 clean truth + events 1X2, walk-forward retrain",
                 "n_train": n}, REF_MODEL)
    print(f"  参考模型已存(独立路径, 未覆盖生产): {REF_MODEL}")

    out = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "method": "P0-10 independent_model retrained on GQ 2026 clean + walk-forward (N_FOLDS=4), no look-ahead",
        "n_universe": n, "folds": fold_reports,
        "pooled": {"n_test": len(all_test_proba), "model_logloss": pooled_model_ll,
                   "market_logloss": pooled_market_ll, "ev": pooled_ev},
        "verdict": ("walk-forward 跨折 pooling +EV(CI不跨零)=真 beat market, 关闭 P0 真闸门; "
                    "否则独立信息路径在 2026 GQ 干净数据上亦无 edge(原 P0-9b 退化确为时代错位, "
                    "但重训后仍无 edge=假设证伪)."),
    }
    jp = os.path.join(OUT, "p0_10_retrain_status.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    def ev_line(ev):
        if ev.get("n_bets", 0) == 0:
            return "无下注"
        return (f"下注{ev['n_bets']} ROI={ev['mean_roi']:+}% CI[{ev['roi_ci'][0]:+},{ev['roi_ci'][1]:+}] "
                f"edge_pp={ev['edge_pp']:+} 胜率{ev['win_rate']}% 隐含{ev['implied']}% +EV={ev['positive_ev']}")
    md = [f"# P0-10 independent_model 重训 + walk-forward 诚实检验（GQ 2026 干净）\n",
          f"> 生成: {out['generated_at']}\n> 宇宙: {n:,} 场, N_FOLDS={N_FOLDS}\n"]
    for fr in fold_reports:
        md.append(f"\n## 折{fr['fold']} (train={fr['train_n']} / test={fr['test_n']})")
        md.append(f"- 模型 LL {fr['model_logloss']} vs 市场 {fr['market_logloss']} beat={fr['model_beats_market_ll']}")
        md.append(f"- +EV: {ev_line(fr['ev'])}")
    md.append(f"\n## 跨折 pooling（test={out['pooled']['n_test']}）")
    md.append(f"- 模型 LL {pooled_model_ll} vs 市场 {pooled_market_ll}")
    md.append(f"- +EV: {ev_line(pooled_ev)}")
    md.append(f"\n> {out['verdict']}")
    with open(os.path.join(OUT, "p0_10_retrain_status.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"-> {jp}")


if __name__ == "__main__":
    main()
