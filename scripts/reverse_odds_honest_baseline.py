#!/usr/bin/env python3
"""
赔率逆向 — 诚实基线 (确立1x2预测真实天花板)
==============================================
严格时序切分, 杜绝数据泄露。对比4个层级, 量化每层真实贡献:
  L0: 收盘隐含概率(市场基线, 零模型)
  L1: 纯赔率特征(开盘/收盘/overround/home_edge)
  L2: L1 + drift特征(三方漂移)
  L3: L2 + drift三方组合模式特征(操盘套路指纹)

铁律: 用2023+做测试集, 训练绝不碰测试期数据。
"""
from __future__ import annotations
import sqlite3, os, json, time, warnings
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, log_loss
from lightgbm import LGBMClassifier
warnings.filterwarnings('ignore')

DB = 'data/football_data.db'
SPLIT = '2023-01-01'


def load():
    c = sqlite3.connect(DB)
    df = pd.read_sql_query(
        'SELECT * FROM odds_features WHERE open_h>0 AND close_h>0 AND outcome IS NOT NULL AND match_date IS NOT NULL',
        c)
    c.close()
    df.match_date = pd.to_datetime(df.match_date)
    df['y'] = df.outcome.map({'H': 0, 'D': 1, 'A': 2})
    return df


def build_features(df, level):
    """构建指定层级的特征。所有特征仅用赔率信息, 不用结果(防泄露)。"""
    f = pd.DataFrame(index=df.index)
    # L1: 纯赔率 (开盘/收盘/隐含概率/overround/home_edge)
    for c in ['open_h','open_d','open_a','close_h','close_d','close_a']:
        f[c] = df[c]
    f['imp_h'] = df.imp_h; f['imp_d'] = df.imp_d; f['imp_a'] = df.imp_a
    f['cimp_h'] = df.cimp_h; f['cimp_d'] = df.cimp_d; f['cimp_a'] = df.cimp_a
    f['overround'] = df.overround
    f['home_edge'] = df.home_edge
    # 赔率比与差
    f['open_ha_ratio'] = df.open_h / df.open_a
    f['close_ha_ratio'] = df.close_h / df.close_a
    f['imp_spread'] = (df.cimp_h - df.cimp_a).abs()
    f['imp_balance'] = df.cimp_h / df.cimp_d  # 主vs平

    if level >= 2:
        # L2: drift 特征
        for c in ['drift_h','drift_d','drift_a']:
            f[c] = df[c]
        f['drift_mag'] = np.maximum.reduce([df.drift_h.abs(), df.drift_d.abs(), df.drift_a.abs()])
        f['drift_h_minus_a'] = df.drift_h - df.drift_a  # 主客漂移差
        f['drift_sum'] = df.drift_h + df.drift_d + df.drift_a
        # 概率漂移 (收盘imp - 开盘imp)
        f['pdrift_h'] = df.cimp_h - df.imp_h
        f['pdrift_d'] = df.cimp_d - df.imp_d
        f['pdrift_a'] = df.cimp_a - df.imp_a
        f['sigma_trap'] = df.sigma_trap

    if level >= 3:
        # L3: drift 三方组合模式 (操盘套路指纹)
        hd = np.sign(df.drift_h)
        dd = np.sign(df.drift_d)
        ad = np.sign(df.drift_a)
        # 只看显著漂移
        hd = np.where(df.drift_h.abs() < 0.02, 0, hd)
        dd = np.where(df.drift_d.abs() < 0.02, 0, dd)
        ad = np.where(df.drift_a.abs() < 0.02, 0, ad)
        # 27种模式 one-hot (实际有效约15种)
        pattern = hd.astype(int) * 9 + dd.astype(int) * 3 + ad.astype(int) + 13  # 映射到0-26
        f['drift_pattern'] = pattern
        # 关键模式二值特征 (探索报告里的高信号模式)
        f['pat_honest_defH'] = ((hd == -1) & (dd == 1) & (ad == 1)).astype(int)    # -111 诚实防H
        f['pat_honest_defA'] = ((hd == 1) & (dd == 1) & (ad == -1)).astype(int)   # 11-1 诚实防A
        f['pat_fake_defH'] = ((hd == -1) & (dd == -1) & (ad == 1)).astype(int)    # -1-11 诱盘假H
        f['pat_all_down'] = ((hd == -1) & (dd == -1) & (ad == -1)).astype(int)    # 全降 资金均压
        # drift不对称性 (主vs客的漂移强度差)
        f['drift_asym_ha'] = df.drift_h.abs() - df.drift_a.abs()
        # 背离度: drift方向与收盘argmax是否一致
        close_argmax = np.argmax(df[['cimp_h','cimp_d','cimp_a']].values, axis=1)
        drift_supports_close = np.zeros(len(df))
        for i, ca in enumerate(close_argmax):
            drifts = [df.drift_h.iloc[i], df.drift_d.iloc[i], df.drift_a.iloc[i]]
            drift_supports_close[i] = 1 if drifts[ca] < 0 else -1  # 收盘argmax方向赔率下调=一致
        f['drift_close_consistency'] = drift_supports_close

    return f.fillna(0).replace([np.inf, -np.inf], 0)


def metrics(y, pred, proba=None):
    acc = accuracy_score(y, pred)
    mf1 = f1_score(y, pred, average='macro', zero_division=0)
    df1 = f1_score(y, pred, labels=[1], average='macro', zero_division=0)
    hf1 = f1_score(y, pred, labels=[0], average='macro', zero_division=0)
    af1 = f1_score(y, pred, labels=[2], average='macro', zero_division=0)
    ll = log_loss(y, np.clip(proba, 1e-9, 1-1e-9)) if proba is not None else None
    return {'acc': round(acc,4), 'macro_f1': round(mf1,4), 'd_f1': round(df1,4),
            'h_f1': round(hf1,4), 'a_f1': round(af1,4), 'logloss': round(ll,4) if ll else None}


def main():
    t0 = time.time()
    print('='*70)
    print('  赔率逆向诚实基线 — 严格时序切分 (训练pre-2023 / 测试2023+)')
    print('='*70)
    df = load()
    train = df[df.match_date < SPLIT].copy()
    test = df[df.match_date >= SPLIT].copy()
    print(f'\n  训练: {len(train)} 场 | 测试: {len(test)} 场')

    results = {}

    # L0: 收盘隐含概率 (市场基线, 零模型)
    print('\n--- L0: 收盘隐含概率 (市场基线) ---')
    test_proba = test[['cimp_h','cimp_d','cimp_a']].values
    # 归一化(去overround)
    test_proba = test_proba / test_proba.sum(axis=1, keepdims=True)
    test_pred = np.argmax(test_proba, axis=1)
    results['L0_收盘价基线'] = metrics(test.y.values, test_pred, test_proba)
    print(f'  {results["L0_收盘价基线"]}')

    # L1-L3: 模型
    for level in [1, 2, 3]:
        names = {1:'L1_纯赔率', 2:'L2_+drift', 3:'L3_+drift组合模式'}
        print(f'\n--- {names[level]} ---')
        Xtr = build_features(train, level)
        Xte = build_features(test, level)
        feat_cols = Xtr.columns.tolist()
        ytr = train.y.values
        yte = test.y.values

        model = LGBMClassifier(
            n_estimators=300, max_depth=6, num_leaves=47,
            learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
            min_child_samples=50, reg_alpha=0.1, reg_lambda=1.0,
            random_state=42, n_jobs=-1, verbose=-1,
        )
        model.fit(Xtr, ytr)
        proba = model.predict_proba(Xte)
        pred = np.argmax(proba, axis=1)
        m = metrics(yte, pred, proba)
        results[names[level]] = m
        print(f'  特征数: {len(feat_cols)} | {m}')

        # 特征重要性 Top10
        if level == 3:
            imp = sorted(zip(feat_cols, model.feature_importances_), key=lambda x:-x[1])[:12]
            print(f'  特征重要性Top12: {[(n,int(v)) for n,v in imp]}')

    # 汇总
    print(f'\n{"="*70}')
    print(f'  诚实基线汇总 (测试集 2023+, {len(test)}场)')
    print(f'{"="*70}')
    print(f'{"层级":<22}{"Acc":>8}{"MacroF1":>9}{"D-F1":>8}{"H-F1":>8}{"A-F1":>8}{"LogLoss":>9}')
    print('-'*70)
    for name, m in results.items():
        print(f'{name:<22}{m["acc"]:>8}{m["macro_f1"]:>9}{m["d_f1"]:>8}{m["h_f1"]:>8}{m["a_f1"]:>8}{m["logloss"] or "":>9}')
    print()
    print(f'  drift贡献 (L2-L1): ΔAcc={results["L2_+drift"]["acc"]-results["L1_纯赔率"]["acc"]:+.4f} '
          f'ΔD-F1={results["L2_+drift"]["d_f1"]-results["L1_纯赔率"]["d_f1"]:+.4f}')
    print(f'  组合模式贡献 (L3-L2): ΔAcc={results["L3_+drift组合模式"]["acc"]-results["L2_+drift"]["acc"]:+.4f} '
          f'ΔD-F1={results["L3_+drift组合模式"]["d_f1"]-results["L2_+drift"]["d_f1"]:+.4f}')
    print(f'  vs市场基线 (L3-L0): ΔAcc={results["L3_+drift组合模式"]["acc"]-results["L0_收盘价基线"]["acc"]:+.4f}')
    print(f'\n  ⏱ {time.time()-t0:.1f}s')

    out = {'split': SPLIT, 'n_train': len(train), 'n_test': len(test), 'results': results}
    with open('reports/reverse_odds_baseline.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f'  📁 reports/reverse_odds_baseline.json')


if __name__ == '__main__':
    main()
