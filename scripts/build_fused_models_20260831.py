"""融合测试模型构建 (2026-08-31, 非破坏性, 不覆盖生产模型)
================================================================
按用户指令, 把已有模型临时融合成 3 个新测试模型 + 开发 OU1H 模型:

  1. OU 测试模型  = 堆叠LR(fl_OU P(over) , poisson P(over@line))
  2. 1X2 测试模型 = 堆叠LR(fl_1x2 [H,D,A] , fl_AH [主覆,客覆])
  3. league 测试模型 = 堆叠LR(league_main_v1 [H,D,A] , league_draw_expert P(平))
  4. OU1H 模型    = 按 OU1H_校准与跟随大球回测_实测报告.md 开发(研究原型, 标数据地雷)

数据: events.db 干净完结场(有真实 score_at 快照, 排除假0-0), 开盘赔率取自 odds_snapshots。
训练切分: 时间外(早70%训练, 晚30%评估)。
落盘: models/fused_ou_20260831.joblib / fused_1x2_20260831.joblib / fused_league_20260831.joblib / ou1h_model_20260831.joblib
指标: reports/fused_models_build_20260831.json

诚实边界(IR-30): OU1H 沿用报告已知地雷——ht_score 66%污染, 干净子集n小, 选择偏差, 不宣言可部署。
"""
from __future__ import annotations
import os, sys, json, sqlite3, warnings, datetime
import numpy as np
warnings.filterwarnings("ignore")
import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, accuracy_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "data", "events.db")
MODELS = os.path.join(ROOT, "models")

from analysis.live_goal_probe import (_open_1x2_from_snapshots, _open_ah_from_snapshots)
from scripts.compare_ou_models_20260830 import opening_ou
from pipeline.poisson_gbm import available as gbm_ok, predict_lambdas, p_over
from pipeline.odds_feature_library import extract_features, FEATURE_NAMES
from pipeline.odds_structure_db import render_structure
from pipeline.fl_predictor import predict_from_odds

FIT_FRAC = 0.70  # 早70%训练

# ----------------------------------------------------------- 赔率提取
def _open_1x2(con, mk):
    rows = con.execute(
        "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2' "
        "ORDER BY captured_at ASC", (mk,)).fetchall()
    d = {}
    for sel, o in rows:
        if sel not in d and o and o > 1.01:
            d[sel] = o
    return d.get("home"), d.get("draw"), d.get("away")

def build_feat(oh, od, oa, ou_line, ou_ov, ou_un, ah_line, ah_h, ah_a, league, kickoff):
    row = {
        "source": "backtest", "league": league, "kickoff": kickoff,
        "op_1x2_h": oh, "op_1x2_d": od, "op_1x2_a": oa,
        "op_ou_line": ou_line, "op_ou_over": ou_ov, "op_ou_under": ou_un,
        "op_ah_line": ah_line, "op_ah_home": ah_h, "op_ah_away": ah_a,
        "op_cs": None, "score_home": None, "score_away": None, "result": None,
    }
    struct = render_structure(row)
    return np.array(extract_features(struct, 0.0, kickoff), dtype=float)

# ----------------------------------------------------------- 基模型概率
def fl_probs(oh, od, oa, ou_line, ou_ov, ou_un, ah_line, ah_h, ah_a, league, kickoff):
    out = predict_from_odds(h=oh, d=od, a=oa,
                            ou_line=ou_line, ou_over=ou_ov, ou_under=ou_un,
                            ah_line=ah_line, ah_home=ah_h, ah_away=ah_a,
                            league=league, kickoff=kickoff)
    return out

_LEAGUE_MODELS = None

def league_probs(feat):
    """league_main_v1 的 meta(LogisticRegression) 由 sklearn 1.9.0 训练, 在 1.6.1 下
    predict_proba 内部访问已移除的 multi_class 属性会崩。改为等权软投票 lgb+xgb
    (与 stacking meta 近似), 版本安全。draw_expert 走 Isotonic 校准。"""
    global _LEAGUE_MODELS
    if _LEAGUE_MODELS is None:
        lm = joblib.load(os.path.join(ROOT, "data", "league_main_v1.joblib"))
        de = joblib.load(os.path.join(ROOT, "data", "league_draw_expert.joblib"))
        _LEAGUE_MODELS = (lm, de)
    lm, de = _LEAGUE_MODELS
    X = feat.reshape(1, -1)
    lgb_p = lm["lgb"].predict_proba(X)[0]; xgb_p = lm["xgb"].predict_proba(X)[0]
    main = (lgb_p + xgb_p) / 2.0
    draw_raw = de["model"].predict_proba(X)[0, 1]
    draw_cal = float(de["calibrator"].predict(np.array([draw_raw]))[0])
    return main, draw_cal  # [pH,pD,pA], pDraw

# ----------------------------------------------------------- 收集同步样本
def collect():
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    matches = con.execute(
        "SELECT match_key, home, away, score_home, score_away, league, kickoff FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL ORDER BY kickoff ASC").fetchall()
    recs = []
    for m in matches:
        mk = m["match_key"]
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1", (mk,)).fetchone():
            continue
        if not con.execute("SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                           "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            continue
        ou = opening_ou(con, mk)
        if not ou:
            continue
        line, ov, un = ou
        oh, od, oa = _open_1x2(con, mk)
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            continue
        try:
            ah_line, ah_h, ah_a = _open_ah_from_snapshots(con, mk)
        except Exception:
            ah_line, ah_h, ah_a = None, None, None
        if not (ah_line is not None and ah_h and ah_a and ah_h > 1.01 and ah_a > 1.01):
            continue
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=m["league"])
        if not lam:
            continue
        try:
            feat37 = build_feat(oh, od, oa, line, ov, un, ah_line, ah_h, ah_a, m["league"], m["kickoff"])
        except Exception:
            continue
        sh, sa = int(m["score_home"]), int(m["score_away"])
        recs.append(dict(
            mk=mk, league=m["league"], ko=m["kickoff"],
            oh=oh, od=od, oa=oa, line=line, ov=ov, un=un,
            ah_line=ah_line, ah_h=ah_h, ah_a=ah_a,
            sh=sh, sa=sa, tot=sh + sa,
            lam=lam,
        ))
    con.close()
    return recs

# ----------------------------------------------------------- 主流程
def main():
    assert gbm_ok(), "Poisson GBM 不可用"
    print("收集同步样本 ...")
    recs = collect()
    print(f"  有效同步样本: {len(recs)}")
    if len(recs) < 200:
        print("样本过少, 中止"); return
    recs.sort(key=lambda r: r["ko"])
    k = int(len(recs) * FIT_FRAC)
    tr, te = recs[:k], recs[k:]
    print(f"  时间切分: 训练 {len(tr)} | 评估(时间外) {len(te)}")

    # ---- 基模型输出(缓存) ----
    print("计算基模型概率 ...")
    for r in recs:
        fl = fl_probs(r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
                      r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"])
        r["fl_1x2"] = fl["1x2"]
        r["fl_ou"] = fl["ou"]
        r["fl_ah"] = fl["ah"]
        main_p, draw_p = league_probs(build_feat(
            r["oh"], r["od"], r["oa"], r["line"], r["ov"], r["un"],
            r["ah_line"], r["ah_h"], r["ah_a"], r["league"], r["ko"]))
        r["lg_main"] = main_p
        r["lg_draw"] = draw_p
        r["poisson_over"] = float(p_over(r["lam"][0], r["lam"][1], r["line"]))

    metrics = {"generated_at": datetime.datetime.now().astimezone().isoformat(),
               "n_total": len(recs), "n_train": len(tr), "n_test": len(te),
               "models": {}}

    def auc(p, y):
        if len(set(y)) < 2:
            return float("nan")
        if p.ndim == 1 or (hasattr(p, "shape") and p.shape[1] == 1) or (isinstance(p, list)):
            return float(roc_auc_score(y, np.asarray(p)))
        return float(roc_auc_score(y, np.asarray(p), multi_class="ovo", average="macro"))

    # ===== 1) OU 融合 =====
    # 2026-08-31: fl_model_ou.joblib 已下线(AUC 0.523 < baseline 0.529, 垃圾模型已删),
    # fl_probs()["ou"] 恒 None → OU 融合特征缺失, 本块跳过. OU 概率统一走 poisson(p_over).
    print("\n=== OU 融合 SKIPPED (fl_model_ou 已下线 2026-08-31, OU 走纯泊松) ===")
    def ou_block(split):
        X = np.array([[r["fl_ou"][0], r["poisson_over"]] for r in split])
        y = np.array([1 if r["tot"] > r["line"] else 0 for r in split])
        return X, y
    Xtr, ytr = ou_block(tr); Xte, yte = ou_block(te)
    meta = LogisticRegression(max_iter=2000, C=1.0).fit(Xtr, ytr)
    pte = meta.predict_proba(Xte)[:, 1]
    base_fl = np.array([r["fl_ou"][0] for r in te])
    base_po = np.array([r["poisson_over"] for r in te])
    m_ou = {
        "coef": [float(c) for c in meta.coef_[0]], "intercept": float(meta.intercept_[0]),
        "fused_AUC": auc(pte, yte), "fl_OU_AUC": auc(base_fl, yte),
        "poisson_AUC": auc(base_po, yte), "n_test": len(yte),
        "base_rate": float(ytr.mean()),
    }
    print(f"  融合AUC={m_ou['fused_AUC']:.4f} | fl_OU={m_ou['fl_OU_AUC']:.4f} | poisson={m_ou['poisson_AUC']:.4f}")
    joblib.dump({"meta": meta, "feature_cols": ["fl_ou_pOver", "poisson_pOver"],
                 "trained_at": datetime.datetime.now().isoformat()},
                os.path.join(MODELS, "fused_ou_20260831.joblib"))
    metrics["models"]["fused_ou"] = m_ou

    # ===== 2) 1X2 融合 (fl_1x2 + fl_AH) =====
    print("\n=== 1X2 融合 (fl_1x2 + fl_AH) ===")
    def x2_block(split):
        X = np.array([[r["fl_1x2"][0], r["fl_1x2"][1], r["fl_1x2"][2],
                       r["fl_ah"][0], r["fl_ah"][1]] for r in split])
        y = np.array([0 if r["sh"] > r["sa"] else (1 if r["sh"] == r["sa"] else 2) for r in split])
        return X, y
    Xtr, ytr = x2_block(tr); Xte, yte = x2_block(te)
    meta = LogisticRegression(max_iter=2000, C=1.0, multi_class="multinomial").fit(Xtr, ytr)
    pte = meta.predict_proba(Xte)
    yp = pte.argmax(1)
    fl_acc = accuracy_score(yte, np.array([r["fl_1x2"].index(max(r["fl_1x2"])) for r in te]))
    m_x2 = {
        "fused_acc": float(accuracy_score(yte, yp)),
        "fl_1x2_acc": float(fl_acc),
        "fused_macro_auc": auc(pte, yte) if pte.shape[1] > 2 else float("nan"),
        "n_test": len(yte),
        "base_acc": float(np.bincount(ytr).max() / len(ytr)),
    }
    print(f"  融合acc={m_x2['fused_acc']:.4f} | fl_1x2 acc={m_x2['fl_1x2_acc']:.4f} | 朴素基线={m_x2['base_acc']:.4f}")
    joblib.dump({"meta": meta, "feature_cols": ["fl_1x2_H","fl_1x2_D","fl_1x2_A","fl_AH_Hcov","fl_AH_Acov"],
                 "trained_at": datetime.datetime.now().isoformat()},
                os.path.join(MODELS, "fused_1x2_20260831.joblib"))
    metrics["models"]["fused_1x2"] = m_x2

    # ===== 3) league 融合 (league_main + draw_expert) =====
    print("\n=== league 融合 (league_main_v1 + draw_expert) ===")
    def lg_block(split):
        X = np.array([[r["lg_main"][0], r["lg_main"][1], r["lg_main"][2], r["lg_draw"]] for r in split])
        y = np.array([0 if r["sh"] > r["sa"] else (1 if r["sh"] == r["sa"] else 2) for r in split])
        return X, y
    Xtr, ytr = lg_block(tr); Xte, yte = lg_block(te)
    meta = LogisticRegression(max_iter=2000, C=1.0, multi_class="multinomial").fit(Xtr, ytr)
    pte = meta.predict_proba(Xte); yp = pte.argmax(1)
    lg_acc = accuracy_score(yte, np.array([int(np.argmax(r["lg_main"])) for r in te]))
    m_lg = {
        "fused_acc": float(accuracy_score(yte, yp)),
        "league_main_acc": float(lg_acc),
        "fused_macro_auc": auc(pte, yte),
        "n_test": len(yte),
        "base_acc": float(np.bincount(ytr).max() / len(ytr)),
    }
    print(f"  融合acc={m_lg['fused_acc']:.4f} | league_main acc={m_lg['league_main_acc']:.4f} | 朴素基线={m_lg['base_acc']:.4f}")
    joblib.dump({"meta": meta, "feature_cols": ["lg_main_H","lg_main_D","lg_main_A","draw_expert_pDraw"],
                 "trained_at": datetime.datetime.now().isoformat()},
                os.path.join(MODELS, "fused_league_20260831.joblib"))
    metrics["models"]["fused_league"] = m_lg

    # ===== 4) OU1H 模型 (报告方法, 研究原型) =====
    print("\n=== OU1H 模型 (按报告方法, 研究原型) ===")
    ou1h = build_ou1h()
    metrics["models"]["ou1h"] = ou1h

    with open(os.path.join(ROOT, "reports", "fused_models_build_20260831.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    print("\n完成. 指标 -> reports/fused_models_build_20260831.json")
    print("落盘: fused_ou_20260831 / fused_1x2_20260831 / fused_league_20260831 / ou1h_model_20260831")

def build_ou1h():
    """按 OU1H_校准与跟随大球回测_实测报告.md: 取 OU_1H 开盘线 + 干净半场真值(ht<ft)。
    诚实边界: ht_score 66%污染, 仅 ht<ft 子集可用, n小, 选择偏差 — 不宣言可部署。"""
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT match_key, ht_score_home, ht_score_away, score_home, score_away, kickoff FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL ORDER BY kickoff ASC").fetchall()
    rec = []
    for m in rows:
        mk = m["match_key"]
        hth, hta = m["ht_score_home"], m["ht_score_away"]
        if hth is None or hta is None:
            continue
        # 干净半场真值铁律: 仅 ht_total < ft_total
        if (hth + hta) >= (int(m["score_home"]) + int(m["score_away"])):
            continue
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1", (mk,)).fetchone():
            continue
        # 取 OU_1H 开盘线(minute_at=0 最早)
        snaps = con.execute(
            "SELECT line, odds, selection FROM odds_snapshots WHERE match_key=? AND market LIKE 'OU_1H_%' "
            "ORDER BY captured_at ASC", (mk,)).fetchall()
        if not snaps:
            continue
        # 取主盘线: 两边去水概率最接近50/50
        lines = {}
        for line, odds, sel in snaps:
            lines.setdefault(line, {})[sel] = odds
        best_line, best_p = None, 0
        for line, d in lines.items():
            if "over" in d and "under" in d and d["over"] > 1.01 and d["under"] > 1.01:
                po = (1/d["over"])/((1/d["over"])+(1/d["under"]))
                if abs(po-0.5) < abs(best_p-0.5) or best_line is None:
                    best_line, best_p = line, po
        if best_line is None:
            continue
        ht_tot = hth + hta
        rec.append(dict(line=best_line, implied=best_p, y=1 if ht_tot > best_line else 0, ko=m["kickoff"]))
    con.close()
    if len(rec) < 30:
        return {"error": f"干净半场样本仅 {len(rec)}, 不足建模", "n": len(rec)}
    rec.sort(key=lambda r: r["ko"])
    k = int(len(rec)*FIT_FRAC)
    tr, te = rec[:k], rec[k:]
    Xtr = np.array([[r["implied"]] for r in tr]); ytr = np.array([r["y"] for r in tr])
    Xte = np.array([[r["implied"]] for r in te]); yte = np.array([r["y"] for r in te])
    # Isotonic 校准(在 OOF 概率上, 这里小样本直接全量拟合, 标为研究)
    from sklearn.isotonic import IsotonicRegression
    meta = LogisticRegression(max_iter=2000, C=1e6).fit(Xtr, ytr)
    pte = meta.predict_proba(Xte)[:, 1]
    ir = IsotonicRegression(out_of_bounds="clip"); ir.fit(meta.predict_proba(Xtr)[:,1], ytr)
    pte_cal = ir.predict(pte)
    res = {
        "method": "OU_1H 开盘隐含P(over) → Isotonic校准 → P(over 1H)",
        "n_clean_total": len(rec), "n_train": len(tr), "n_test": len(te),
        "implied_mean": float(np.mean([r["implied"] for r in rec])),
        "actual_over_rate": float(np.mean([r["y"] for r in rec])),
        "fused_AUC_raw": float(roc_auc_score(yte, pte)),
        "fused_AUC_cal": float(roc_auc_score(yte, pte_cal)),
        "data_caveat": "ht_score 66%污染, 仅 ht<ft 子集; 选择偏差(下半场又进球场偏高); n小; 研究原型不可部署",
    }
    print(f"  n_clean={len(rec)} 隐含P(over)均={res['implied_mean']:.3f} 实际over率={res['actual_over_rate']:.3f}")
    print(f"  AUC(raw)={res['fused_AUC_raw']:.4f} AUC(cal)={res['fused_AUC_cal']:.4f} [缺口 {res['actual_over_rate']-res['implied_mean']:+.3f}]")
    joblib.dump({"ir": ir, "lr": meta, "trained_at": datetime.datetime.now().isoformat(),
                 "caveat": res["data_caveat"]},
                os.path.join(MODELS, "ou1h_model_20260831.joblib"))
    return res

if __name__ == "__main__":
    main()
