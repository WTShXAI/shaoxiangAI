"""
哨响AI · 特征库标准评估 (重复CV + AUC + 概率分箱)
==================================================
消费 shaoxiang_feature_library.db，评估 1X2 / OU / AH 三个"正确选项"分类器。

为什么不用 train_feature_library_model.py 的单次 walkforward + accuracy:
  (铁律7, 2026-08-03 实测确立)
  1) 单次 train/test split 在小样本上约 3% 概率误报退化 —— AH 曾被误报 -4.1pp,
     重复CV 真值是 +7.6pp 且显著为正。
  2) accuracy 在类别不平衡任务上系统性低估模型 —— OU 多数类占 71.5%,
     accuracy 增益看似 -0.1pp, 但 AUC=0.672 是三任务最高。
  => 主指标必须是 AUC + 概率分箱单调性; accuracy 仅作参考并必须并排 naive 基线。

模型选择 (铁律, 2026-08-03 实测):
  去水概率单纯形(三概率和≡1)上 LightGBM/HistGBM 集体失效,
  必须用 sklearn DecisionTreeClassifier(max_depth=3); 喂前 np.nan_to_num。

用法:
  python scripts/eval_feature_library_cv.py            # 默认 30x5 折
  python scripts/eval_feature_library_cv.py --repeats 10
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, r"D:\Architecture")

import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import RepeatedStratifiedKFold, StratifiedKFold, cross_val_predict
from sklearn.metrics import roc_auc_score, accuracy_score

from pipeline.odds_feature_library import FEATURE_NAMES, N_FEAT

FEAT_DB = r"D:\Architecture\data\shaoxiang_feature_library.db"
TASKS = ("1x2", "ou", "ah")
TASK_CN = {"1x2": "1X2 独赢", "ou": "OU 大小球", "ah": "AH 让球"}
# 各任务类别含义 (与 pipeline.odds_feature_library._LABEL1X2 / _LABELSIDE 对齐)
CLASS_CN = {
    "1x2": {0: "主胜", 1: "平局", 2: "客胜"},
    "ou": {0: "大球", 1: "小球"},
    "ah": {0: "主让赢盘", 1: "客让赢盘"},
}


#: 基础特征 = 纯赔率结构, 历次版本都有。跨库(新/旧备份)A-B 对比时只能用它,
#: 否则会把"特征集变化"和"数据修正"两个变量混在一起。
BASIC_FEATURES = [n for n in FEATURE_NAMES if not n.startswith("ftick_")]


def load_task(task: str, db: str = None, basic: bool = False):
    """从特征库读出某任务的 (X, y, kickoff, raw_1x2_odds)。

    basic=True 时只取 BASIC_FEATURES; 否则取库中实际存在的 FEATURE_NAMES
    (老备份库缺 ftick_* 列, 自动降级而非报错)。
    """
    con = sqlite3.connect(f"file:{db or FEAT_DB}?mode=ro", uri=True)
    have = {r[1] for r in con.execute("PRAGMA table_info(features)")}
    want = BASIC_FEATURES if basic else FEATURE_NAMES
    use = [c for c in want if c in have]
    n_feat = len(use)
    cols = ", ".join(use)
    rows = con.execute(
        f"SELECT {cols}, label_1x2, label_ou, label_ah, kickoff FROM features"
    ).fetchall()
    con.close()

    lab_idx = {"1x2": n_feat, "ou": n_feat + 1, "ah": n_feat + 2}[task]
    X, y, ks, odds = [], [], [], []
    for r in rows:
        lab = r[lab_idx]
        if lab is None:
            continue
        X.append([float(v) if v is not None else np.nan for v in r[:n_feat]])
        y.append(int(lab))
        ks.append(r[n_feat + 3] or "")
        odds.append((r[0], r[1], r[2]))          # x1_h / x1_d / x1_a
    X = np.nan_to_num(np.asarray(X, dtype=float))  # 铁律: 喂前必须 nan_to_num
    return X, np.asarray(y), ks, np.asarray(odds, dtype=object)


def auc_of(y, proba):
    """二分类取正类概率; 多分类用 ovr-macro。"""
    n_cls = proba.shape[1]
    if n_cls == 2:
        return roc_auc_score(y, proba[:, 1])
    return roc_auc_score(y, proba, multi_class="ovr", average="macro")


def repeated_cv(X, y, repeats: int, seed: int = 0):
    """30x5 重复分层 CV, 返回每折的 (acc, auc, naive_acc)。"""
    rskf = RepeatedStratifiedKFold(n_splits=5, n_repeats=repeats, random_state=seed)
    accs, aucs, bases = [], [], []
    for tr, te in rskf.split(X, y):
        clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=20, random_state=0)
        clf.fit(X[tr], y[tr])
        proba = clf.predict_proba(X[te])
        accs.append(accuracy_score(y[te], clf.predict(X[te])))
        try:
            aucs.append(auc_of(y[te], proba))
        except ValueError:
            pass                                    # 该折某类缺失, 跳过 AUC
        maj = np.bincount(y[tr]).argmax()           # naive = 训练集多数类
        bases.append(float(np.mean(y[te] == maj)))
    return np.array(accs), np.array(aucs), np.array(bases)


def single_split_walkforward(X, y, ks):
    """单次 walkforward (早70%训/晚30%测) —— 仅用于印证铁律7, 不作为结论。"""
    order = np.argsort(np.asarray(ks))
    Xs, ys = X[order], y[order]
    cut = int(len(ys) * 0.7)
    if cut < 10 or len(ys) - cut < 10:
        return None
    clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=20, random_state=0)
    clf.fit(Xs[:cut], ys[:cut])
    acc = accuracy_score(ys[cut:], clf.predict(Xs[cut:]))
    maj = np.bincount(ys[:cut]).argmax()
    base = float(np.mean(ys[cut:] == maj))
    return acc, base


def quintile_table(X, y, task: str):
    """out-of-fold 概率五分箱: 检验概率是否单调可用于出信号。"""
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=0)
    clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=20, random_state=0)
    proba = cross_val_predict(clf, X, y, cv=skf, method="predict_proba")

    if proba.shape[1] == 2:
        score = proba[:, 1]                      # P(class=1)
        hit = (y == 1).astype(float)
        label = f"P({CLASS_CN[task][1]})"
        hit_label = f"实际{CLASS_CN[task][1]}率"
    else:
        score = proba.max(axis=1)                # 置信度
        hit = (proba.argmax(axis=1) == y).astype(float)
        label = "P(预测类) 置信度"
        hit_label = "实际命中率"

    # 用 rank 分箱, 规避大量并列值导致 qcut 失败
    ranks = np.argsort(np.argsort(score, kind="stable"), kind="stable")
    bins = np.minimum((ranks * 5) // len(score), 4)
    out = []
    for b in range(5):
        m = bins == b
        if m.sum() == 0:
            out.append((b, 0, np.nan, np.nan))
            continue
        out.append((b, int(m.sum()), float(score[m].mean()), float(hit[m].mean())))
    return label, hit_label, out, float(hit.mean())


def run_task(task: str, repeats: int, db: str = None, basic: bool = False):
    X, y, ks, _ = load_task(task, db, basic)
    n = len(y)
    print(f"\n{'='*70}")
    print(f"【{TASK_CN[task]}】样本 {n} 场 | 特征 {X.shape[1]} 维")
    dist = {CLASS_CN[task].get(int(c), c): int((y == c).sum()) for c in np.unique(y)}
    print(f"  类别分布: {dist}")
    if n < 50:
        print("  样本不足 50, 跳过")
        return None

    accs, aucs, bases = repeated_cv(X, y, repeats)
    gain = accs - bases
    # 增益的 t 统计 (折间), 判断是否稳定为正
    t_stat = gain.mean() / (gain.std(ddof=1) / np.sqrt(len(gain))) if gain.std(ddof=1) > 0 else 0.0

    print(f"\n  ── 重复CV ({repeats}x5={len(accs)} 折) ──")
    print(f"  模型 accuracy : {accs.mean()*100:6.2f}%  (±{accs.std()*100:.2f})")
    print(f"  naive 多数类  : {bases.mean()*100:6.2f}%  (±{bases.std()*100:.2f})")
    print(f"  增益          : {gain.mean()*100:+6.2f}pp  t={t_stat:5.1f}  "
          f"{'显著' if abs(t_stat) > 2 else '不显著'}")
    if len(aucs):
        auc_t = (aucs.mean() - 0.5) / (aucs.std(ddof=1) / np.sqrt(len(aucs)))
        print(f"  AUC (主指标)  : {aucs.mean():6.4f}  (±{aucs.std():.4f})  "
              f"vs 随机 0.5  t={auc_t:5.1f}  "
              f"{'有区分度' if aucs.mean() > 0.55 else '弱' if aucs.mean() > 0.52 else '无'}")

    sw = single_split_walkforward(X, y, ks)
    if sw:
        print(f"\n  ── 单次 walkforward 对照 (铁律7: 仅参考, 不作结论) ──")
        print(f"  模型 {sw[0]*100:.2f}% / naive {sw[1]*100:.2f}% "
              f"→ 增益 {(sw[0]-sw[1])*100:+.2f}pp"
              f"{'   ⚠ 与重复CV结论相反!' if (sw[0]-sw[1])*gain.mean() < 0 else ''}")

    label, hit_label, table, overall = quintile_table(X, y, task)
    print(f"\n  ── out-of-fold 概率五分箱 ({label}) ──")
    print(f"  {'分箱':<6}{'样本':>6}{'  均值':>10}{'  '+hit_label:>14}{'  vs整体':>10}")
    for b, cnt, sc, hr in table:
        if cnt == 0:
            continue
        print(f"  Q{b+1:<5}{cnt:>6}{sc:>10.3f}{hr*100:>13.1f}%{(hr-overall)*100:>+9.1f}pp")
    valid = [r for r in table if r[1] > 0]
    if len(valid) >= 2:
        spread = (valid[-1][3] - valid[0][3]) * 100
        hrs = [r[3] for r in valid]
        mono = all(hrs[i] <= hrs[i + 1] for i in range(len(hrs) - 1)) or \
               all(hrs[i] >= hrs[i + 1] for i in range(len(hrs) - 1))
        print(f"  整体 {overall*100:.1f}% | 顶底价差 {spread:+.1f}pp | "
              f"单调性: {'完美单调 ✅ 全箱可用' if mono else '非单调 ⚠ 只吃 Q1/Q5 两端'}")

    return {"task": task, "n": n, "acc": accs.mean(), "base": bases.mean(),
            "gain": gain.mean(), "t": t_stat,
            "auc": aucs.mean() if len(aucs) else float("nan")}


def main():
    ap = argparse.ArgumentParser(description="哨响AI 特征库标准评估 (重复CV+AUC+分箱)")
    ap.add_argument("--repeats", type=int, default=30, help="CV 重复次数 (默认30)")
    ap.add_argument("--db", default=None,
                    help="指定特征库路径 (默认当前库; 可指向 .bak_* 做前后对比)")
    ap.add_argument("--basic", action="store_true",
                    help="只用 BASIC_FEATURES (不含 ftick_*), 用于新旧库公平 A/B")
    args = ap.parse_args()

    db = args.db or FEAT_DB
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    total = con.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    con.close()
    print("=" * 70)
    print("哨响AI · 特征库标准评估 (重复CV + AUC + 概率分箱)")
    print(f"特征库: {db}")
    print(f"总样本: {total} 场 | 模型: DecisionTree(max_depth=3, min_samples_leaf=20)")
    if args.basic:
        print(f"特征集: BASIC (纯赔率结构 {len(BASIC_FEATURES)} 维, 已剔除 ftick_*) [A/B 模式]")
    print("=" * 70)

    res = [r for r in (run_task(t, args.repeats, db, args.basic) for t in TASKS) if r]

    print(f"\n{'='*70}")
    print("汇总 (主指标 = AUC)")
    print(f"{'任务':<12}{'样本':>7}{'AUC':>9}{'模型acc':>10}{'naive':>9}{'增益':>10}{'显著':>7}")
    for r in res:
        print(f"{TASK_CN[r['task']]:<12}{r['n']:>7}{r['auc']:>9.4f}"
              f"{r['acc']*100:>9.1f}%{r['base']*100:>8.1f}%"
              f"{r['gain']*100:>+9.2f}pp{'  是' if abs(r['t']) > 2 else '  否':>7}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
