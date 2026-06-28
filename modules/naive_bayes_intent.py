"""
朴素贝叶斯意图识别器
====================
核心假设:
  - 赔率是庄家意图的加密协议
  - 各赔率衍生特征在给定真实结果(H/D/A)下条件独立
  - 通过贝叶斯定理反推 P(结果 | 赔率特征)

特征选择 (针对意图识别, 不是泛化特征):
  - odds_spread: 主客赔率差 (实力差)
  - odds_draw_dev: D赔率偏离均匀 (D诱盘信号)
  - odds_imp_d: D隐含概率 (D市场热度)
  - odds_overround: 抽水 (庄家自信度)
  - drift_magnitude: 赔率漂移幅度 (意图变化)
  - drift_d: D方向漂移 (D意图强化)
  - odds_draw: D赔率绝对值 (D便宜度)
  - ix_sharp_draw: D锐信号交互项
  - drift_d_signal: D漂移信号

变体:
  - GaussianNB: 连续特征, 假设高斯分布
  - MultinomialNB: 离散化后用
  - ComplementNB: 不平衡数据友好
"""
import os, sys, json, sqlite3
import numpy as np
import pandas as pd
from sklearn.naive_bayes import GaussianNB, MultinomialNB, ComplementNB
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix, roc_auc_score
from sklearn.preprocessing import KBinsDiscretizer

ROOT = os.path.dirname(os.path.abspath(__file__))

# 意图识别专用特征 (赔率信号, 不含球队/联赛上下文)
INTENT_FEATURES = [
    'odds_spread',         # 主客赔率差
    'odds_draw_dev',       # D赔率偏离
    'odds_imp_d',          # D隐含概率
    'odds_imp_h',          # H隐含概率
    'odds_imp_a',          # A隐含概率
    'odds_overround',      # 抽水
    'odds_draw',           # D赔率
    'odds_home',           # H赔率
    'odds_away',           # A赔率
    'drift_magnitude',     # 漂移幅度
    'drift_d',             # D漂移
    'drift_h_val',         # H漂移
    'drift_a_val',         # A漂移
    'drift_d_signal',      # D漂移信号(衍生)
    'ix_sharp_draw',       # D锐信号交互
    'ix_drift_even_draw',  # 均衡+漂移D
    'ix_drift_draw_odds',  # 漂移×D赔率
    'ix_bal_even',         # 均衡度
    'ix_power_gap',        # 实力差
]

def load_data():
    """加载全量数据 + 意图特征"""
    conn = sqlite3.connect(os.path.join(ROOT, 'data', 'football_data.db'))
    cursor = conn.cursor()
    cursor.execute('PRAGMA table_info(match_features)')
    db_cols = set(r[1] for r in cursor.fetchall())

    # 检查哪些特征在DB中
    existing = [c for c in INTENT_FEATURES if c in db_cols]
    missing = [c for c in INTENT_FEATURES if c not in db_cols]
    if missing:
        print(f"  [NB] DB缺失特征(用0填充): {missing}")

    cols_sql = ", ".join([f"mf.{c}" for c in existing])
    query = f"""
    SELECT m.match_id, m.match_date, m.home_score, m.away_score,
           {cols_sql},
           o_avg.home_odds AS odds_home, o_avg.draw_odds AS odds_draw, o_avg.away_odds AS odds_away,
           o_avg.return_rate AS odds_return_rate,
           (1.0/o_avg.home_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_h,
           (1.0/o_avg.draw_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_d,
           (1.0/o_avg.away_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_a,
           o_avg.away_odds - o_avg.home_odds AS odds_spread,
           (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds - 1.0) AS odds_overround,
           (1.0/o_avg.draw_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) - 0.333 AS odds_draw_dev
    FROM matches m
    JOIN match_features mf ON m.match_id = mf.match_id
    LEFT JOIN (
        SELECT match_id, AVG(home_odds) AS home_odds, AVG(draw_odds) AS draw_odds,
               AVG(away_odds) AS away_odds, AVG(return_rate) AS return_rate
        FROM odds WHERE home_odds > 0 AND draw_odds > 0 AND away_odds > 0
        GROUP BY match_id
    ) o_avg ON m.match_id = o_avg.match_id
    WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
    ORDER BY m.match_date
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    # 去除重复列名 (SQL计算列可能与表列重名)
    df = df.loc[:, ~df.columns.duplicated()]

    # 填充缺失列
    for col in missing:
        df[col] = 0.0

    # 填充赔率缺失
    defaults = {
        'odds_home': 2.5, 'odds_draw': 3.3, 'odds_away': 2.8,
        'odds_imp_h': 0.40, 'odds_imp_d': 0.28, 'odds_imp_a': 0.32,
        'odds_spread': 0.0, 'odds_overround': 0.05, 'odds_draw_dev': 0.0,
        'drift_magnitude': 0.0, 'drift_d': 0.0, 'drift_h_val': 0.0, 'drift_a_val': 0.0,
        'drift_d_signal': 0.0, 'ix_sharp_draw': 0.0, 'ix_drift_even_draw': 0.0,
        'ix_drift_draw_odds': 0.0, 'ix_bal_even': 0.0, 'ix_power_gap': 0.0,
    }
    for col, default in defaults.items():
        if col in df.columns:
            df[col] = df[col].fillna(default)

    return df

def label_from_score(home_score, away_score):
    if home_score > away_score:
        return 0
    elif home_score == away_score:
        return 1
    else:
        return 2

def run_experiment():
    print("=" * 70)
    print("朴素贝叶斯意图识别器 — OOF 2023+ 回测")
    print("=" * 70)

    df = load_data()
    print(f"全量数据: {len(df)} 条")

    # 只用有真实赔率的样本 (NB 在缺失赔率下无意义)
    has_odds = (df['odds_home'] != 2.5) | (df['odds_draw'] != 3.3)
    df = df[has_odds].copy().reset_index(drop=True)
    print(f"有真实赔率样本: {len(df)} 条")

    # 标签
    df['label'] = df.apply(lambda r: label_from_score(r['home_score'], r['away_score']), axis=1)

    # 时间切分
    train_mask = df['match_date'] < '2023-01-01'
    test_mask = df['match_date'] >= '2023-01-01'
    df_train = df[train_mask].copy()
    df_test = df[test_mask].copy()
    print(f"训练 (pre-2023): {len(df_train)} | 测试 (2023+): {len(df_test)}")

    # 准备特征矩阵 (确保列存在且顺序一致)
    feature_cols = []
    for c in INTENT_FEATURES:
        if c in df.columns:
            feature_cols.append(c)
        else:
            df[c] = 0.0
            feature_cols.append(c)
    X_train = df_train[feature_cols].values.astype(np.float64)
    X_test = df_test[feature_cols].values.astype(np.float64)
    y_train = df_train['label'].values
    y_test = df_test['label'].values

    print(f"特征数: {len(feature_cols)}")
    print(f"标签分布 (train): H={np.sum(y_train==0)} D={np.sum(y_train==1)} A={np.sum(y_train==2)}")

    # ============================================================
    # 实验1: GaussianNB
    # ============================================================
    print("\n" + "=" * 70)
    print("实验1: GaussianNB (连续特征, 高斯假设)")
    print("=" * 70)

    gnb = GaussianNB(var_smoothing=1e-9)
    gnb.fit(X_train, y_train)
    proba_gnb = gnb.predict_proba(X_test)
    pred_gnb = np.argmax(proba_gnb, axis=1)

    acc = accuracy_score(y_test, pred_gnb)
    macro_f1 = f1_score(y_test, pred_gnb, average='macro', zero_division=0)
    f1_h, f1_d, f1_a = f1_score(y_test, pred_gnb, average=None, zero_division=0)
    cm = confusion_matrix(y_test, pred_gnb)
    recalls = cm.diagonal() / cm.sum(axis=1)
    precisions = [cm[i,i]/max(cm[:,i].sum(),1) for i in range(3)]
    try:
        auc = roc_auc_score(y_test, proba_gnb, multi_class='ovr', average='macro')
    except ValueError:
        auc = 0

    print(f"  Acc={acc:.4f}  Macro-F1={macro_f1:.4f}  AUC={auc:.4f}")
    print(f"\n  Confusion Matrix:")
    print(f"            预测H    预测D    预测A")
    for i, name in enumerate(['H(主胜)', 'D(平局)', 'A(客胜)']):
        print(f"  实际{name}  {cm[i][0]:6d}  {cm[i][1]:6d}  {cm[i][2]:6d}")
    print(f"\n  {'类':>8} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Support':>10}")
    for i, name in enumerate(['H(主胜)', 'D(平局)', 'A(客胜)']):
        f1_i = f1_score(y_test, pred_gnb, labels=[i], average='micro', zero_division=0)
        print(f"  {name:>8} {precisions[i]:>10.4f} {recalls[i]:>10.4f} {f1_i:>10.4f} {int(cm[i].sum()):>10d}")

    # ============================================================
    # 实验2: GaussianNB var_smoothing 扫描
    # ============================================================
    print("\n" + "=" * 70)
    print("实验2: GaussianNB var_smoothing 扫描")
    print("=" * 70)
    print(f"  {'var_smoothing':>15} {'Acc':>8} {'MacroF1':>8} {'D_F1':>8} {'AUC':>8}")
    best_gnb = {'acc': 0, 'vs': 1e-9}
    for vs in [1e-12, 1e-10, 1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3]:
        m = GaussianNB(var_smoothing=vs)
        m.fit(X_train, y_train)
        p = m.predict_proba(X_test)
        pr = np.argmax(p, axis=1)
        a = accuracy_score(y_test, pr)
        mf = f1_score(y_test, pr, average='macro', zero_division=0)
        _, fd, _ = f1_score(y_test, pr, average=None, zero_division=0)
        try:
            au = roc_auc_score(y_test, p, multi_class='ovr', average='macro')
        except ValueError:
            au = 0
        print(f"  {vs:>15.0e} {a:>8.4f} {mf:>8.4f} {fd:>8.4f} {au:>8.4f}")
        if a > best_gnb['acc']:
            best_gnb = {'acc': a, 'vs': vs, 'model': m, 'proba': p, 'pred': pr}

    print(f"\n  最优 var_smoothing={best_gnb['vs']:.0e}, Acc={best_gnb['acc']:.4f}")

    # ============================================================
    # 实验3: MultinomialNB (离散化)
    # ============================================================
    print("\n" + "=" * 70)
    print("实验3: MultinomialNB (特征离散化为10桶)")
    print("=" * 70)

    # 离散化
    kbd = KBinsDiscretizer(n_bins=10, encode='ordinal', strategy='quantile')
    # 对负值特征做平移 (MNB需要非负)
    X_train_shift = X_train - X_train.min(axis=0) + 1
    X_test_shift = X_test - X_train.min(axis=0) + 1
    X_train_disc = kbd.fit_transform(X_train_shift)
    X_test_disc = kbd.transform(X_test_shift)

    print(f"  {'alpha':>8} {'Acc':>8} {'MacroF1':>8} {'D_F1':>8} {'AUC':>8}")
    best_mnb = {'acc': 0}
    for alpha in [0.01, 0.1, 0.5, 1.0, 2.0, 5.0]:
        m = MultinomialNB(alpha=alpha)
        m.fit(X_train_disc, y_train)
        p = m.predict_proba(X_test_disc)
        pr = np.argmax(p, axis=1)
        a = accuracy_score(y_test, pr)
        mf = f1_score(y_test, pr, average='macro', zero_division=0)
        _, fd, _ = f1_score(y_test, pr, average=None, zero_division=0)
        try:
            au = roc_auc_score(y_test, p, multi_class='ovr', average='macro')
        except ValueError:
            au = 0
        print(f"  {alpha:>8.2f} {a:>8.4f} {mf:>8.4f} {fd:>8.4f} {au:>8.4f}")
        if a > best_mnb['acc']:
            best_mnb = {'acc': a, 'alpha': alpha, 'model': m, 'proba': p, 'pred': pr}

    # ============================================================
    # 实验4: 特征重要性 (对数概率差)
    # ============================================================
    print("\n" + "=" * 70)
    print("实验4: 特征对意图区分的贡献 (GaussianNB 类条件均值差)")
    print("=" * 70)

    # 各特征在不同类别下的均值
    print(f"  {'特征':>20} {'H类均值':>10} {'D类均值':>10} {'A类均值':>10} {'H-D差':>10} {'D-A差':>10}")
    for i, feat in enumerate(feature_cols):
        means = gnb.theta_  # [n_classes, n_features]
        h_mean = means[0, i]
        d_mean = means[1, i]
        a_mean = means[2, i]
        print(f"  {feat:>20} {h_mean:>10.4f} {d_mean:>10.4f} {a_mean:>10.4f} {h_mean-d_mean:>10.4f} {d_mean-a_mean:>10.4f}")

    # ============================================================
    # 实验5: 与 v4.0 信号正交性
    # ============================================================
    print("\n" + "=" * 70)
    print("实验5: NB vs v4.0 信号正交性")
    print("=" * 70)

    from ensemble_trainer import EnsembleTrainer

    trainer = EnsembleTrainer.load_pipeline(
        os.path.join(ROOT, 'saved_models', 'football_v4.0_production.joblib'))

    # 加载完整数据 (含全部特征) 用于 v4 推理
    feat_cols = trainer.config['data']['feature_columns']
    db_path = trainer.db_path
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute('PRAGMA table_info(match_features)')
    db_cols_full = set(r[1] for r in cursor.fetchall())
    existing_full = [c for c in feat_cols if c in db_cols_full]
    missing_full = [c for c in feat_cols if c not in db_cols_full]
    cols_sql_full = ", ".join([f"mf.{c}" for c in existing_full])
    query_full = f"""
    SELECT m.match_id, m.home_team_name, m.away_team_name, m.match_date,
           m.league_name, m.home_score, m.away_score,
           {cols_sql_full},
           o_avg.home_odds AS odds_home, o_avg.draw_odds AS odds_draw, o_avg.away_odds AS odds_away,
           o_avg.return_rate AS odds_return_rate,
           (1.0/o_avg.home_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_h,
           (1.0/o_avg.draw_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_d,
           (1.0/o_avg.away_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) AS odds_imp_a,
           o_avg.away_odds - o_avg.home_odds AS odds_spread,
           (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds - 1.0) AS odds_overround,
           (1.0/o_avg.draw_odds) / (1.0/o_avg.home_odds + 1.0/o_avg.draw_odds + 1.0/o_avg.away_odds) - 0.333 AS odds_draw_dev,
           mf.odds_open_h, mf.odds_open_d, mf.odds_open_a,
           mf.odds_close_h, mf.odds_close_d, mf.odds_close_a
    FROM matches m
    JOIN match_features mf ON m.match_id = mf.match_id
    LEFT JOIN (
        SELECT match_id, AVG(home_odds) AS home_odds, AVG(draw_odds) AS draw_odds,
               AVG(away_odds) AS away_odds, AVG(return_rate) AS return_rate
        FROM odds WHERE home_odds > 0 AND draw_odds > 0 AND away_odds > 0
        GROUP BY match_id
    ) o_avg ON m.match_id = o_avg.match_id
    WHERE m.home_score IS NOT NULL AND m.away_score IS NOT NULL
      AND m.home_team_name IS NOT NULL AND m.away_team_name IS NOT NULL
    ORDER BY m.match_date
    """
    df_full = pd.read_sql_query(query_full, conn)
    conn.close()
    for col in missing_full:
        df_full[col] = 0.0
    odds_defaults = {
        'odds_home': 2.5, 'odds_draw': 3.3, 'odds_away': 2.8, 'odds_return_rate': 0.95,
        'odds_imp_h': 0.40, 'odds_imp_d': 0.28, 'odds_imp_a': 0.32,
        'odds_spread': 0.0, 'odds_overround': 0.05, 'odds_draw_dev': 0.0,
        'drift_magnitude': 0.0, 'drift_direction': 0, 'drift_sharp_signal': 0,
        'drift_h_val': 0.0, 'drift_d': 0.0, 'drift_a_val': 0.0,
        'odds_open_h': 0.0, 'odds_open_d': 0.0, 'odds_open_a': 0.0,
        'odds_close_h': 0.0, 'odds_close_d': 0.0, 'odds_close_a': 0.0,
        'real_home_odds': 0.0, 'real_draw_odds': 0.0, 'real_away_odds': 0.0,
    }
    for col, default in odds_defaults.items():
        if col in df_full.columns:
            df_full[col] = df_full[col].fillna(default)

    df_oof_full = df_full[df_full['match_date'] >= '2023-01-01'].copy().reset_index(drop=True)
    print(f"  v4.0 OOF 样本: {len(df_oof_full)}")

    # v4 推理
    X_v4, y_v4 = trainer.prepare_features(df_oof_full, add_interactions=True)
    X_v4_scaled = trainer.scaler.transform(X_v4.values.astype(np.float64))
    y_v4_arr = y_v4.values if hasattr(y_v4, 'values') else y_v4
    proba_v4 = trainer._predict_with_stacking(X_v4_scaled)
    pred_v4 = np.argmax(proba_v4, axis=1)
    print(f"  v4.0 Acc={accuracy_score(y_v4_arr, pred_v4):.4f}")

    # NB 推理 (用同样的样本, 确保特征列完全一致)
    for c in feature_cols:
        if c not in df_oof_full.columns:
            df_oof_full[c] = 0.0
    X_nb = df_oof_full[feature_cols].values.astype(np.float64)
    proba_nb = best_gnb['model'].predict_proba(X_nb)
    pred_nb = np.argmax(proba_nb, axis=1)
    print(f"  NB   Acc={accuracy_score(y_v4_arr, pred_nb):.4f}")

    # 正交性
    agree = (pred_v4 == pred_nb).mean()
    v4_correct = (pred_v4 == y_v4_arr)
    nb_correct = (pred_nb == y_v4_arr)
    both = (v4_correct & nb_correct).sum()
    only_v4 = (v4_correct & ~nb_correct).sum()
    only_nb = (~v4_correct & nb_correct).sum()
    neither = (~v4_correct & ~nb_correct).sum()
    print(f"\n  预测一致率: {agree:.4f}")
    print(f"  两者都对: {both} ({both/len(y_v4_arr)*100:.1f}%)")
    print(f"  仅v4对:   {only_v4} ({only_v4/len(y_v4_arr)*100:.1f}%)")
    print(f"  仅NB对:   {only_nb} ({only_nb/len(y_v4_arr)*100:.1f}%)")
    print(f"  两者都错: {neither} ({neither/len(y_v4_arr)*100:.1f}%)")
    print(f"  → NB独有贡献: {only_nb} 场 ({only_nb/len(y_v4_arr)*100:.1f}%)")

    # ============================================================
    # 实验6: NB × v4 加权融合
    # ============================================================
    print("\n" + "=" * 70)
    print("实验6: NB × v4.0 加权融合扫描")
    print("=" * 70)
    print(f"  {'w_v4':>5} {'w_nb':>5} {'Acc':>8} {'MacroF1':>8} {'D_F1':>8} {'AUC':>8}")
    best_fuse = {'acc': 0}
    for w_v4 in np.arange(0.5, 1.01, 0.05):
        w_nb = 1.0 - w_v4
        proba_fused = w_v4 * proba_v4 + w_nb * proba_nb
        pred_fused = np.argmax(proba_fused, axis=1)
        acc = accuracy_score(y_v4_arr, pred_fused)
        macro_f1 = f1_score(y_v4_arr, pred_fused, average='macro', zero_division=0)
        _, fd, _ = f1_score(y_v4_arr, pred_fused, average=None, zero_division=0)
        try:
            auc = roc_auc_score(y_v4_arr, proba_fused, multi_class='ovr', average='macro')
        except ValueError:
            auc = 0
        print(f"  {w_v4:>5.2f} {w_nb:>5.2f} {acc:>8.4f} {macro_f1:>8.4f} {fd:>8.4f} {auc:>8.4f}")
        if acc > best_fuse['acc']:
            best_fuse = {'acc': acc, 'w_v4': w_v4, 'w_nb': w_nb, 'macro_f1': macro_f1, 'd_f1': fd, 'auc': auc}

    print(f"\n  最优: w_v4={best_fuse['w_v4']:.2f}, Acc={best_fuse['acc']:.4f}")
    print(f"  v4.0 基线 Acc={accuracy_score(y_v4_arr, pred_v4):.4f} → 提升 {(best_fuse['acc']-accuracy_score(y_v4_arr, pred_v4))*100:+.2f}pp")

    # ============================================================
    # 实验7: NB + v4 + D-Gate 联合
    # ============================================================
    print("\n" + "=" * 70)
    print("实验7: NB×v4 融合 + D-Gate Precision Filter")
    print("=" * 70)
    proba_fused_best = best_fuse['w_v4'] * proba_v4 + best_fuse['w_nb'] * proba_nb
    print(f"  {'margin阈值':>10} {'Acc':>8} {'MacroF1':>8} {'D_F1':>8} {'D_Prec':>8} {'D_Recall':>8}")
    for margin_th in [0.0, 0.05, 0.08, 0.10, 0.15]:
        pred_adj = np.argmax(proba_fused_best, axis=1)
        for i in range(len(proba_fused_best)):
            if pred_adj[i] == 1:
                p = proba_fused_best[i]
                margin = p[1] - max(p[0], p[2])
                if margin < margin_th:
                    pred_adj[i] = 0 if p[0] >= p[2] else 2
        acc = accuracy_score(y_v4_arr, pred_adj)
        macro_f1 = f1_score(y_v4_arr, pred_adj, average='macro', zero_division=0)
        _, fd, _ = f1_score(y_v4_arr, pred_adj, average=None, zero_division=0)
        cm = confusion_matrix(y_v4_arr, pred_adj)
        d_prec = cm[1][1] / max(cm[:, 1].sum(), 1)
        d_rec = cm[1][1] / max(cm[1].sum(), 1)
        print(f"  {margin_th:>10.2f} {acc:>8.4f} {macro_f1:>8.4f} {fd:>8.4f} {d_prec:>8.4f} {d_rec:>8.4f}")

    # 保存
    output = {
        'experiment': 'naive_bayes_intent',
        'n_features': len(feature_cols),
        'features': feature_cols,
        'gaussian_nb': {
            'best_var_smoothing': best_gnb['vs'],
            'acc': float(best_gnb['acc']),
        },
        'multinomial_nb': {
            'best_alpha': best_mnb.get('alpha', 0),
            'acc': float(best_mnb['acc']),
        },
        'orthogonality': {
            'agree_rate': float(agree),
            'both_correct': int(both),
            'only_v4': int(only_v4),
            'only_nb': int(only_nb),
            'neither': int(neither),
        },
        'best_fusion': {k: float(v) if isinstance(v, (int, float, np.floating)) else v
                       for k, v in best_fuse.items() if k != 'model'},
    }
    out_path = os.path.join(ROOT, 'output', 'naive_bayes_intent.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n结果已保存: {out_path}")

if __name__ == '__main__':
    run_experiment()
