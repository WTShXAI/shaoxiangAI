# -*- coding: utf-8 -*-
"""
WC 真跨赛事 walk-forward (届间先验特征)
======================================
训练: 更早届(结果派生特征, 无泄漏) -> 测试: 更晚届
对照: ML vs 多数类基线 vs 赔率argmax(有赔率子集)
回答: ML 跨届泛化是否优于基线? (与 #1 结论对照)

用法: .venv/Scripts/python.exe scripts/wc_cross_tournament_wf.py
"""
import sqlite3, os, json, numpy as np
from collections import Counter
try:
    import lightgbm as lgb
except ImportError:
    from sklearn.ensemble import RandomForestClassifier as lgb  # 兜底
from sklearn.metrics import accuracy_score, f1_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, 'data', 'football_data.db')
FEAT = ['home_prior_gp', 'home_prior_pts', 'home_prior_gf', 'home_prior_ga',
        'away_prior_gp', 'away_prior_pts', 'away_prior_gf', 'away_prior_ga',
        'h2h_hw', 'h2h_d', 'h2h_aw', 'h2h_n', 'stage_group', 'prior_available',
        # 丰富特征 (B)
        'home_intra_gf', 'home_intra_ga', 'home_intra_pts',
        'away_intra_gf', 'away_intra_ga', 'away_intra_pts',
        'imp_h', 'imp_d', 'imp_a', 'prior_pts_diff']
YMAP = {'H': 0, 'D': 1, 'A': 2}
INV = {0: 'H', 1: 'D', 2: 'A'}


def load(editions):
    c = sqlite3.connect(DB)
    q = (f"SELECT m.final_result, {','.join('f.'+f for f in FEAT)}, m.oh,m.od,m.oa "
         f"FROM wc_all_matches m JOIN wc_features f ON m.id=f.match_id "
         f"WHERE m.edition IN ({','.join('?'*len(editions))})")
    rows = c.execute(q, editions).fetchall()
    c.close()
    X, y, odds = [], [], []
    for r in rows:
        fr = r[0]; feats = r[1:1+len(FEAT)]; oh, od, oa = r[1+len(FEAT):1+len(FEAT)+3]
        X.append([0.0 if v is None else float(v) for v in feats])
        y.append(YMAP[fr]); odds.append((oh, od, oa))
    return np.array(X), np.array(y), odds


def argmax_acc(y, odds):
    tot = correct = 0
    for yi, (oh, od, oa) in zip(y, odds):
        if oh is None or od is None or oa is None:
            continue
        tot += 1
        pred = 0 if oh == min(oh, od, oa) else (1 if od == min(oh, od, oa) else 2)
        if pred == yi:
            correct += 1
    return (correct / tot if tot else None, tot)


def run(train_eds, test_eds, label):
    Xtr, ytr, _ = load(train_eds)
    Xte, yte, odte = load(test_eds)
    clf = lgb.LGBMClassifier(n_estimators=200, num_leaves=15, learning_rate=0.05,
                             random_state=42, n_jobs=-1) if hasattr(lgb, 'LGBMClassifier') \
        else lgb(n_estimators=200, random_state=42, n_jobs=-1)
    clf.fit(Xtr, ytr)
    pred = clf.predict(Xte)
    acc = accuracy_score(yte, pred)
    macro = f1_score(yte, pred, average='macro')
    draw = f1_score(yte, pred, labels=[1], average='macro')
    maj = Counter(ytr).most_common(1)[0][0]
    maj_acc = (yte == maj).mean()
    am, nt = argmax_acc(yte, odte)
    importances = {}
    if hasattr(clf, 'feature_importances_'):
        importances = {f: round(float(v), 4) for f, v in zip(FEAT, clf.feature_importances_)}
    return {
        'label': label, 'train': train_eds, 'test': test_eds,
        'n_train': len(ytr), 'n_test': len(yte),
        'ml_acc': round(float(acc), 4), 'macro_f1': round(float(macro), 4),
        'draw_f1': round(float(draw), 4),
        'majority_acc': round(float(maj_acc), 4),
        'argmax_acc': round(float(am), 4) if am is not None else None,
        'argmax_n': nt,
        'importances': importances,
    }


def main():
    results = []
    # 主测试: 历史三届 -> 2026
    results.append(run(['2014', '2018', '2022'], ['2026'], '历史三届(14/18/22) -> 2026'))
    # 干净版: 仅含先验届 -> 2026
    results.append(run(['2018', '2022'], ['2026'], '仅先验届(18/22) -> 2026'))
    # expanding-window 逐届
    results.append(run(['2014'], ['2018'], '2014 -> 2018'))
    results.append(run(['2014', '2018'], ['2022'], '2014+2018 -> 2022'))
    results.append(run(['2014', '2018', '2022'], ['2026'], '2014+2018+2022 -> 2026'))

    print("=" * 70)
    print("WC 真跨赛事 walk-forward 结果")
    print("=" * 70)
    for r in results:
        am = f"{r['argmax_acc']:.3f}(n={r['argmax_n']})" if r['argmax_acc'] is not None else "n/a"
        print(f"\n[{r['label']}]  train={r['n_train']} test={r['n_test']}")
        print(f"  ML={r['ml_acc']:.3f}  多数类={r['majority_acc']:.3f}  赔率argmax={am}")
        print(f"  macroF1={r['macro_f1']:.3f}  drawF1={r['draw_f1']:.3f}")
    # 结论行
    main_test = results[0]
    print("\n>>> 主测试(历史三届->2026): ML=%s vs 多数类=%s vs 赔率argmax=%s" % (
        main_test['ml_acc'], main_test['majority_acc'], main_test['argmax_acc']))
    # 主测试特征重要性 (top-8)
    if main_test.get('importances'):
        top = sorted(main_test['importances'].items(), key=lambda x: -x[1])[:8]
        print("    特征重要性 top8:", ", ".join(f"{k}={v:.3f}" for k, v in top))
    os.makedirs(os.path.join(ROOT, 'deliverables'), exist_ok=True)
    out = os.path.join(ROOT, 'deliverables', 'wc2026_cross_tournament_wf.json')
    json.dump(results, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print("saved", out)


if __name__ == '__main__':
    main()
