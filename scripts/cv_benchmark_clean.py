"""
cv_benchmark_clean.py — P1 验证闸门 (30×5 分组 CV AUC)
========================================================
在「已剔除采集截断」的干净特征库上, 用文档基准配置 DecisionTree(max_depth=3)
跑 30 重复 × 5 折 GroupKFold (group=kickoff日期, 防未来泄露), 算 1X2/OU AUC。
闸门: AUC 须显著 > 0.5 (有信号) 且不低于历史基准量级。

注: 干净后样本量(2361/1688)少于记忆基准(3275/2413), AUC 绝对数会略变,
重点是确认"剔除截断后模型信号不崩、且优于随机"。
"""
import sys, sqlite3
sys.path.insert(0, "D:/Architecture")
import numpy as np
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import GroupKFold, cross_val_score

FL_DB = "D:/Architecture/data/shaoxiang_feature_library.db"
FEAT = ["x1_h","x1_d","x1_a","x1_margin","x1_fav","x1_drawgap","x1_homefav","x1_hminusa",
        "xou_line","xou_over","xou_under","xou_margin","xou_has",
        "xah_line","xah_home","xah_has","xcs_top1","xcs_ent","xcs_cnt","xcs_has",
        "x_league_freq","x_kickoff_band",
        "ftick_home_trap_04","ftick_away_trap_04","ftick_home_strong_129","ftick_away_strong_129",
        "ftick_any_trap","ftick_any_strong","ftick_home_double_edge","ftick_away_double_edge",
        "xah_away","xah_gap","xah_line_abs","xah_line_bucket","xah_fav_align","xah_margin","xah_home_str"]


def main():
    con = sqlite3.connect(FL_DB)
    rows = con.execute(
        f"SELECT {','.join(FEAT)}, label_1x2, label_ou, label_ah, kickoff FROM features"
    ).fetchall()
    con.close()
    cols = FEAT + ["label_1x2", "label_ou", "label_ah", "kickoff"]
    import numpy as np
    arr = np.array(rows, dtype=object)
    X = arr[:, :len(FEAT)].astype(float)
    def _coerce(col):
        out = []
        for v in arr[:, col]:
            out.append(-1 if v is None else int(v))
        return np.array(out, dtype=int)
    y1 = _coerce(len(FEAT)+0)
    yO = _coerce(len(FEAT)+1)
    kick = np.array([str(r).split(" ")[0] if r else "x" for r in arr[:, -1]])
    print(f"样本矩阵: X={X.shape}, 1x2标签={int((y1>=0).sum())}, OU标签={int((yO>=0).sum())}")

    for name, y in [("1x2", y1), ("ou", yO)]:
        m = y >= 0
        Xm, ym, km = X[m], y[m], kick[m]
        if Xm.shape[0] < 50:
            print(f"  [{name}] 样本不足, 跳过"); continue
        # 1X2 为 3 类标签 -> roc_auc_ovo; OU 为二类 -> roc_auc (ovo 亦可用)
        scoring = "roc_auc_ovo" if name == "1x2" else "roc_auc"
        gkf = GroupKFold(n_splits=5)
        clf = DecisionTreeClassifier(max_depth=3, min_samples_leaf=20, random_state=0)
        aucs = []
        rng = np.random.RandomState(42)
        for rep in range(30):
            idx = rng.permutation(len(ym))
            Xp, yp, kp = Xm[idx], ym[idx], km[idx]
            try:
                s = cross_val_score(clf, Xp, yp, groups=kp, cv=gkf, scoring=scoring)
                aucs.append(np.nanmean(s))
            except Exception as e:
                print(f"  [{name}] rep{rep} err: {e}")
        aucs = np.array(aucs)
        maj = float(np.bincount(ym).max() / len(ym))
        print(f"  [{name}] n={len(ym)} 30×5 GroupKFold AUC = {aucs.mean():.4f} ± {aucs.std():.4f}  "
              f"| 多数类基线acc={maj:.4f}  | 信号增益={(aucs.mean()-0.5):+.4f}")


if __name__ == "__main__":
    main()
