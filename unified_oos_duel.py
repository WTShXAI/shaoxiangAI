# -*- coding: utf-8 -*-
"""
统一 OOS 回测 · 双系统准确率对决
=================================
共享数据集 : D:/Architecture/data/football_data.db :: historical_matches (312,016 场, 2012-2025)
OOS 设计    : 训练窗 match_date < 2023-01-01 (~272k) / 测试窗 >= 2023-01-01 (~39k)
通用输入    : 收盘 1X2 赔率 (close_home/draw/away_odds)
目标        : final_result ∈ {H,D,A}

参赛方:
  A. 本系统 (D:\Architecture)  — odds_structure_classifier 方法论
     devig(等比例去水) + 0.05 网格分桶 + 精确桶(>=20)/最近邻TOP-K 频率画像,
     仅用训练窗建桶, 测试窗 OOS 应用 (不泄露测试标签)。
  B. GitHub shaoxiangAI 仓     — agents/heuristic_predictor.py :: HeuristicPredictor
     纯 numpy 规则型 odds→probs (feature_weight=0.30 + odds_weight=0.70, SP R1/R6 规则),
     冻结仓唯一可无头运行入口 (其 ML 模型 football_v4.1_production.joblib 锁文件不在仓内)。
基线         : 庄家去水隐含概率 (naive implied), 即 v74 交叉核验中的 naive 基线参照。

指标: 宏平均 AUC(OvR) / LogLoss / Brier(多类) / Top-1 准确率。
"""
import os, sys, json, math, sqlite3
from collections import Counter, defaultdict
import numpy as np

ROOT = r"D:\Architecture"
GH   = r"C:\Users\ShXAI\Documents\GitHub\shaoxiangAI"
DB   = os.path.join(ROOT, "data", "football_data.db")
CUT  = "2023-01-01"   # OOS 切分点
MIN_SAMPLE = 20
K_NEIGH = 40

# ---- 复用本系统真实方法论 (devig + bucket_key) ----
sys.path.insert(0, ROOT)
from odds_structure_classifier import devig, bucket_key_str   # 系统真实代码

# ---- GitHub 预测器 (纯 numpy, 无头) ----
sys.path.insert(0, os.path.join(GH, "agents"))
from heuristic_predictor import HeuristicPredictor

LABELS = ["H", "D", "A"]
LIDX   = {"H": 0, "D": 1, "A": 2}

def fetch():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    q = """SELECT close_home_odds, close_draw_odds, close_away_odds, final_result
           FROM historical_matches
           WHERE close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01
             AND final_result IN ('H','D','A') AND match_date IS NOT NULL"""
    cur.execute(q + " AND match_date < ?", (CUT,))
    train = cur.fetchall()
    cur.execute(q + " AND match_date >= ?", (CUT,))
    test  = cur.fetchall()
    con.close()
    return train, test

def build_buckets(train):
    """复用系统方法论: 0.05网格分桶, 存 (h,d,a,outcome) 列表 + 聚合。"""
    bm = defaultdict(list)
    for h, d, a, res in train:
        bm[bucket_key_str(h, d, a)].append((h, d, a, res))
    # 聚合每个桶: 代表赔率=桶中心, 计数, 频率
    reps, Hc, Dc, Ac, Nc = [], [], [], [], []
    for key, lst in bm.items():
        n = len(lst)
        hs = sum(x[0] for x in lst) / n
        ds = sum(x[1] for x in lst) / n
        as_ = sum(x[2] for x in lst) / n
        cnt = Counter(x[3] for x in lst)
        reps.append((hs, ds, as_)); Hc.append(cnt.get("H",0)); Dc.append(cnt.get("D",0)); Ac.append(cnt.get("A",0)); Nc.append(n)
    return np.array(reps, dtype=float), np.array(Hc,float), np.array(Dc,float), np.array(Ac,float), np.array(Nc,float)

def predict_system(test, reps, Hc, Dc, Ac, Nc):
    """本系统: 精确桶(>=MIN_SAMPLE) 否则最近邻 TOP-K 频率画像 (OOS)。"""
    preds = []
    valid = Nc >= MIN_SAMPLE
    R = reps[valid]; Hh = Hc[valid]; Dd = Dc[valid]; Aa = Ac[valid]; Nn = Nc[valid]
    for h, d, a, _ in test:
        key = bucket_key_str(h, d, a)
        # 精确桶不可得时, 统一用最近邻 TOP-K 频率画像 (OOS, 仅训练窗桶)
        dist = ((R[:,0]-h)**2 + (R[:,1]-d)**2 + (R[:,2]-a)**2)
        order = np.argsort(dist)[:K_NEIGH]
        # 仅取有计数的桶
        tot = Nn[order].sum()
        if tot < MIN_SAMPLE:
            # 退化: 退到去水隐含
            ph, pd, pa = devig(h, d, a)
            preds.append([ph, pd, pa]); continue
        ph = Hh[order].sum() / tot
        pd = Dd[order].sum() / tot
        pa = Aa[order].sum() / tot
        s = ph+pd+pa
        preds.append([ph/s, pd/s, pa/s])
    return np.array(preds)

def predict_github(test):
    """GitHub: HeuristicPredictor 无头 odds→probs (其真实冻结仓入口)。"""
    hp = HeuristicPredictor()
    X = np.zeros((1, 1))
    out = []
    for h, d, a, _ in test:
        p = hp.predict_proba(X, feature_names=[], odds_data={"home": h, "draw": d, "away": a}, league_name="")  # noqa
        out.append([float(p[0][0]), float(p[0][1]), float(p[0][2])])
    return np.array(out)

def predict_naive(test):
    out = []
    for h, d, a, _ in test:
        ph, pd, pa = devig(h, d, a)
        out.append([ph, pd, pa])
    return np.array(out)

def metrics(y_true, y_score):
    from sklearn.metrics import roc_auc_score, log_loss
    auc = roc_auc_score(y_true, y_score, multi_class="ovr", average="macro", labels=[0,1,2])
    ll  = log_loss(y_true, y_score, labels=[0,1,2])
    # 多类 Brier = 每样本 sum((onehot-p)^2) 均值
    onehot = np.zeros_like(y_score); onehot[np.arange(len(y_true)), y_true] = 1.0
    brier = ((onehot - y_score)**2).sum(axis=1).mean()
    acc = (y_score.argmax(axis=1) == y_true).mean()
    # 每类 AUC
    per = {}
    for i, lab in enumerate(LABELS):
        try:
            per[lab] = roc_auc_score((y_true==i).astype(int), y_score[:, i])
        except Exception:
            per[lab] = float("nan")
    return dict(auc=float(auc), logloss=float(ll), brier=float(brier), acc=float(acc), per_class_auc=per)

def main():
    train, test = fetch()
    print(f"[data] train={len(train)}  test={len(test)}  (cut={CUT})")
    y_true = np.array([LIDX[r] for *_, r in test])

    # 本系统
    reps, Hc, Dc, Ac, Nc = build_buckets(train)
    print(f"[system] buckets(total)={len(reps)}  buckets(n>={MIN_SAMPLE})={(Nc>=MIN_SAMPLE).sum()}")
    ps = predict_system(test, reps, Hc, Dc, Ac, Nc)
    # GitHub
    pg = predict_github(test)
    # naive
    pn = predict_naive(test)

    ms = metrics(y_true, ps)
    mg = metrics(y_true, pg)
    mn = metrics(y_true, pn)
    # 多数类基线
    majority = Counter(r for *_, r in test).most_common(1)[0][1] / len(test)

    result = {
        "cut": CUT, "n_train": len(train), "n_test": len(test),
        "majority_baseline_acc": round(majority, 4),
        "system_D_Architecture": ms,
        "github_shaoxiangAI_heuristic": mg,
        "naive_implied": mn,
    }
    print("\n================ 统一 OOS 对决 (测试窗 >= %s, n=%d) ================" % (CUT, len(test)))
    hdr = f"{'系统':<32}{'AUC':>8}{'LogLoss':>10}{'Brier':>9}{'Acc':>8}"
    print(hdr); print("-"*len(hdr))
    for name, m in [("本系统(经验桶 OOS)", ms), ("GitHub(HeuristicPredictor)", mg), ("基线(去水隐含)", mn)]:
        print(f"{name:<30}{m['auc']:>8.4f}{m['logloss']:>10.4f}{m['brier']:>9.4f}{m['acc']:>8.3f}")
    print(f"{'多数类基线':<30}{'':>8}{'':>10}{'':>9}{majority:>8.3f}")
    print("\n每类 AUC:")
    for name, m in [("本系统", ms), ("GitHub", mg), ("基线", mn)]:
        pc = m['per_class_auc']
        print(f"  {name:<12} H={pc['H']:.3f} D={pc['D']:.3f} A={pc['A']:.3f}")

    outp = r"D:\Architecture\deliverables\unified_oos_duel_result.json"
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[ok] 结果已存 {outp}")

if __name__ == "__main__":
    main()
