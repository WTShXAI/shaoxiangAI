#!/usr/bin/env python
"""
哨响AI — WC2026 28场真实赔率回测 (解锁 Task #24)
================================================
数据来源: pipeline/archive/validate_wc2026.py
  - 赔率: digital-sanctuary.net 赛前赔率 (真实, 非模拟)
  - 赛果: FIFA 官方 (真实)
  - 特征: DB match_features (77维, 重训后对齐)

目的: 验证重训模型 + 规则层在真实WC比赛上的流水线命中率
禁止: 使用任何模拟/虚拟赔率
"""
import sys, importlib.util, json
from collections import Counter
sys.path.insert(0, 'pipeline'); sys.path.insert(0, 'pipeline/archive')
import numpy as np
import wc_engine as W
from wc_engine import MatchInput

spec = importlib.util.spec_from_file_location('vw', 'pipeline/archive/validate_wc2026.py')
vw = importlib.util.module_from_spec(spec); spec.loader.exec_module(vw)
WC = vw.WC2026

BASELINE_ACC = 0.5769  # v3.2/v4.0/v4.1 旧基线 (26场WC验证)

def main():
    W._load_main(); W._load_de()
    feat_ok = 0
    correct_o = correct_r = 0
    d_actual = d_pred_o = d_pred_r = d_correct_o = d_correct_r = 0
    opt_ne_rule = 0          # optimized 与 rule 判决不同的场数 (ML融合真正生效)
    ml_agree_rule = 0        # 主模型独立预测与规则一致
    details = []

    for (date, home, away, hs, aws, result, ho, do, ao) in WC:
        m = MatchInput(home, away, ho, do, ao, 0.0, 2.5, 'group')
        ro = W.predict(m, mode='optimized')
        rr = W.predict(m, mode='rule')
        po, pr = ro.prediction, rr.prediction

        # 特征可用性 (ML是否真取到)
        feats = W._get_wc_features(home, away)
        if feats is not None:
            feat_ok += 1
            # 主模型独立预测
            pkg = W._MAIN_PKG
            lgb_p = pkg['lgb'].predict_proba([feats])[0]
            xgb_p = pkg['xgb'].predict_proba([feats])[0]
            meta_p = pkg['meta'].predict_proba([np.hstack([lgb_p, xgb_p])])[0]
            ml_pred = ['H', 'D', 'A'][int(np.argmax(meta_p))]
            ml_conf = float(meta_p.max())
        else:
            ml_pred, ml_conf = None, 0.0

        if po == result: correct_o += 1
        if pr == result: correct_r += 1
        if result == 'D':
            d_actual += 1
            if po == 'D': d_correct_o += 1
            if pr == 'D': d_correct_r += 1
        if po == 'D': d_pred_o += 1
        if pr == 'D': d_pred_r += 1
        if po != pr: opt_ne_rule += 1
        if ml_pred is not None and ml_pred == pr: ml_agree_rule += 1

        details.append({
            'date': date, 'home': home, 'away': away,
            'odds': f'{ho}/{do}/{ao}', 'actual': result,
            'opt': po, 'rule': pr, 'ml': ml_pred, 'ml_conf': round(ml_conf, 3),
            'hit_opt': po == result, 'hit_rule': pr == result,
            'feat': feats is not None,
        })

    n = len(details)
    acc_o, acc_r = correct_o / n, correct_r / n
    dr_o = d_correct_o / d_actual if d_actual else 0
    dr_r = d_correct_r / d_actual if d_actual else 0
    dp_o = d_correct_o / d_pred_o if d_pred_o else 0
    dp_r = d_correct_r / d_pred_r if d_pred_r else 0

    print('=' * 92)
    print(f'WC2026 28场 真实赔率流水线回测 (特征可用 {feat_ok}/{n})')
    print('=' * 92)
    print(f'  optimized : 准确率 {correct_o}/{n}={acc_o:.1%}  (旧基线 {BASELINE_ACC:.1%}, uplift {acc_o-BASELINE_ACC:+.1%})')
    print(f'  rule      : 准确率 {correct_r}/{n}={acc_r:.1%}')
    print(f'  D召回(opt/rule): {dr_o:.1%} / {dr_r:.1%} | D精确(opt/rule): {dp_o:.1%} / {dp_r:.1%} | D实际={d_actual}')
    print(f'  ML融合生效场数 (opt≠rule): {opt_ne_rule}/{n}')
    print(f'  主模型与规则一致场数: {ml_agree_rule}/{feat_ok}')
    print('-' * 92)
    print(f'{"#":>3} {"日期":6} {"主队":10} {"客队":10} {"赔率":>16} {"实":3} {"opt":3} {"rule":3} {"ML":3} {"特":3}')
    for i, d in enumerate(details):
        print(f'{i+1:3d} {d["date"]:6} {d["home"]:10} {d["away"]:10} {d["odds"]:>16} {d["actual"]:3} '
              f'{d["opt"]:3} {d["rule"]:3} {d["ml"] or "-":3} {"Y" if d["feat"] else "N":3}')

    # 写报告
    rep = {
        'name': 'wc2026_28_real_odds_backtest',
        'n': n, 'baseline_acc': BASELINE_ACC,
        'optimized_acc': round(acc_o, 4), 'rule_acc': round(acc_r, 4),
        'acc_uplift_vs_baseline': round(acc_o - BASELINE_ACC, 4),
        'd_recall_opt': round(dr_o, 4), 'd_recall_rule': round(dr_r, 4),
        'd_precision_opt': round(dp_o, 4), 'd_precision_rule': round(dp_r, 4),
        'd_actual': d_actual,
        'feature_available': feat_ok,
        'ml_fusion_effective_games': opt_ne_rule,
        'details': details,
    }
    out = 'deliverables/wc2026_28_backtest.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(f'\n✅ 报告: {out}')

if __name__ == '__main__':
    main()
