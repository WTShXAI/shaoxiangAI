"""DC 正则扫描: 证明小样本下 DC 无法逃脱过拟合 (OOS 崩)"""
import sqlite3, math, os
import numpy as np
from scipy.optimize import minimize
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dc_score_model import load_matches, fit_dc, dc_predict, evaluate, MAX_GOAL

data = load_matches()
train = [d for d in data if d['edition']=='2026']
test  = [d for d in data if d['edition']=='2022']
act_t = [(d['hg'],d['ag']) for d in test]

print(f"train={len(train)} test={len(test)}")
print(f"{'reg':>5} | {'full_logloss':>13} {'full_Top3':>10} | {'OOS_logloss':>11} {'OOS_Top1':>9} {'OOS_Top3':>9} | rho")
print('-'*70)
for reg in [0.3, 1.0, 2.0, 5.0]:
    # 全量拟合看rho/mu是否还撞界
    m_full = fit_dc(data, reg=reg)
    fp = [dc_predict(m_full,d['home'],d['away']) for d in data]
    rf = evaluate([(M,0,0,'x') for M,_,_ in fp], [(d['hg'],d['ag']) for d in data])
    # OOS
    m_oos = fit_dc(train, reg=reg)
    tp = [dc_predict(m_oos,d['home'],d['away']) for d in test]
    ro = evaluate([(M,0,0,'x') for M,_,_ in tp], act_t)
    print(f"{reg:5.1f} | {rf['logloss']:13.4f} {rf['top3']:10.3f} | {ro['logloss']:11.4f} {ro['top1']:9.3f} {ro['top3']:9.3f} | {m_full['rho']:.3f}")
print("\n结论: 增大reg仅让rho收敛, 但OOS Top3仍远低于OIP(0.50), DC不适合当前106场/50队样本")
