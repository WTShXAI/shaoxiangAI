#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M3 — 1X2 组件概率堆叠权重 CV 优化器 (时间有序, 防前视)

目标: 在共享干净标注集(odds_features, 316k 行, 含 open/close 赔率 + outcome)上,
      对热路径两路可用组件 wi_teacher + devig_raw 做凸组合权重 CV 优化,
      确认当前生产权重(wi 主导)是否近最优, 抑或可调优.

铁律(见 MEMORY):
  - 命中率必须并排 naive 基线(基线A=永远主胜; 基线B=庄家热门); 仅超过基线B才算 edge.
  - 评估用时间有序 CV(禁随机切分防前视) + AUC + 分箱校准.
  - independent 因与 odds_features 队名词表重叠仅 0.3%, 不在本集堆叠(留作 guarded residual).

输出: 最优凸权重 w_wi (devig=1-w_wi), 及其 logloss/Brier/argmax-acc/macro-AUC,
      并对比 当前生产(0.7225/0.1275) / 纯wi / 纯devig / 两基线.
"""
import sqlite3, sys, os, time, json, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from sklearn.metrics import log_loss, brier_score_loss, roc_auc_score

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "football_data.db")
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deliverables", "m3_wi_cache.npz")
o2i = {'H':0,'D':1,'A':2}
t0 = time.time()

def devig(h,d,a):
    inv=1/h+1/d+1/a; return (1/h)/inv,(1/d)/inv,(1/a)/inv

def load_data():
    c = sqlite3.connect(DB, timeout=30)
    rows = c.execute(
        "SELECT home_team, away_team, open_h, open_d, open_a, close_h, close_d, close_a, outcome, match_date "
        "FROM odds_features WHERE open_h>1.01 AND open_d>1.01 AND open_a>1.01 "
        "AND close_h>1.01 AND close_d>1.01 AND close_a>1.01 AND outcome IN ('H','D','A') "
        "ORDER BY match_date ASC"
    ).fetchall()
    c.close()
    return rows

def get_probs(rows):
    """返回 (Y, devig_mat, wi_mat) 缓存到 npz 加速 CV."""
    if os.path.exists(CACHE):
        z = np.load(CACHE)
        return z['Y'], z['devig'], z['wi']
    n = len(rows)
    Y = np.array([o2i[r[8]] for r in rows])
    devig_mat = np.zeros((n,3)); wi_mat = np.zeros((n,3))
    from pipeline.william_inter_model import predict_1x2 as wi_pred
    for i,r in enumerate(rows):
        ph,pd,pa = devig(r[5],r[6],r[7]); devig_mat[i]=[ph,pd,pa]
        p = wi_pred(r[2],r[3],r[4], r[5],r[6],r[7])
        if p: wi_mat[i]=[p['H'],p['D'],p['A']]
        else: wi_mat[i]=[1/3,1/3,1/3]
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    np.savez(CACHE, Y=Y, devig=devig_mat, wi=wi_mat)
    print(f"[cache] wi probs saved ({time.time()-t0:.0f}s)")
    return Y, devig_mat, wi_mat

def blend(wi_mat, devig_mat, w_wi):
    out = w_wi*wi_mat + (1-w_wi)*devig_mat
    s = out.sum(1, keepdims=True); return out/s

def metrics(P, Y):
    ll = log_loss(Y, P, labels=[0,1,2])
    br = np.mean([brier_score_loss((Y==k).astype(int), P[:,k]) for k in range(3)])
    pred = P.argmax(1)
    acc = (pred==Y).mean()
    try:
        auc = roc_auc_score(np.eye(3)[Y], P, multi_class='ovr', average='macro')
    except Exception:
        auc = float('nan')
    return ll, br, acc, auc

def main():
    rows = load_data()
    Y, devig_mat, wi_mat = get_probs(rows)
    n = len(rows)
    print(f"[data] n={n:,}  date-range {rows[0][9]}..{rows[-1][9]}  ({time.time()-t0:.0f}s)")

    # 时间有序 5-fold: 累计训练权重 + 评估下一段
    NF = 5
    seg = n // NF
    weights = np.linspace(0.5, 1.0, 11)  # w_wi 候选
    fold_best = []
    for f in range(NF-1):
        tr_end = (f+1)*seg
        va_end = (f+2)*seg if f+2 < NF else n
        tr, va = slice(0, tr_end), slice(tr_end, va_end)
        # 训练段选最优 w (logloss)
        best_w, best_ll = 1.0, 1e9
        for w in weights:
            P = blend(wi_mat[tr], devig_mat[tr], w)
            ll,_ ,_ ,_ = metrics(P, Y[tr])
            if ll < best_ll: best_ll, best_w = ll, w
        # 评估段
        Pv = blend(wi_mat[va], devig_mat[va], best_w)
        ll,br,acc,auc = metrics(Pv, Y[va])
        fold_best.append((best_w, ll, br, acc, auc))
        print(f"[fold {f+1}] train_w*={best_w:.2f} | val logloss={ll:.4f} brier={br:.4f} acc={acc:.4f} macroAUC={auc:.4f} n_val={va.stop-va.start:,}")

    bw = np.mean([x[0] for x in fold_best])
    print(f"\n[CV-optimal] mean w_wi across folds = {bw:.3f}  (devig=1-w)")

    # 对比方案 (全量时间有序评估: 前80%训权重, 后20%评估)
    tr, va = slice(0, int(n*0.8)), slice(int(n*0.8), n)
    nv = va.stop - va.start
    scen = {}
    scen['CV_optimal'] = blend(wi_mat[va], devig_mat[va], bw)
    scen['current_prod(0.7225/0.1275)'] = blend(wi_mat[va], devig_mat[va], 0.7225)
    scen['pure_wi(1.0)'] = blend(wi_mat[va], devig_mat[va], 1.0)
    scen['pure_devig(0.0)'] = blend(wi_mat[va], devig_mat[va], 0.0)
    # 基线
    book_fav = devig_mat[va].argmax(1); baseB = (book_fav==Y[va]).mean()
    baseA = (np.zeros(nv)==Y[va]).mean()  # 永远主胜
    print(f"\n[holdout n={nv:,}] naive: always-H acc={baseA:.4f}  book-fav acc={baseB:.4f}")
    print(f"{'scenario':<32}{'logloss':>9}{'brier':>9}{'acc':>9}{'macroAUC':>10}{'vsBaseB':>9}")
    for name,P in scen.items():
        ll,br,acc,auc = metrics(P, Y[va])
        print(f"{name:<32}{ll:>9.4f}{br:>9.4f}{acc:>9.4f}{auc:>10.4f}{acc-baseB:>+9.4f}")

    out = {
        "cv_optimal_w_wi": round(float(bw),3),
        "naive_always_H_acc": round(float(baseA),4),
        "naive_book_fav_acc": round(float(baseB),4),
        "holdout": {name: (lambda m: {"logloss":round(m[0],4),"brier":round(m[1],4),"acc":round(m[2],4),"macro_auc":round(m[3],4),"acc_vs_baseB":round(m[2]-baseB,4)})(metrics(P,Y[va])) for name,P in scen.items()},
        "wi_argmax_acc_full": round(float((wi_mat.argmax(1)==Y).mean()),4),
        "devig_argmax_acc_full": round(float((devig_mat.argmax(1)==Y).mean()),4),
    }
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(os.path.join(os.path.dirname(CACHE), "m3_cv_result.json"), "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n[done] result -> deliverables/m3_cv_result.json  ({time.time()-t0:.0f}s)")

if __name__ == "__main__":
    main()
