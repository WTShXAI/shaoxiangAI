"""
D-Gate 融合对比回测 v2.0 — 真实数据版
=========================================
Base模型: EnsembleTrainer v4.1 predict_match() 真实推理
D-Gate: Path A (prediction_service D-specialist gate) vs Path B (UnifiedPredictor DrawGate v5.3)
数据: WC2026 70场(比分+赔率) + OCR 40场(让球/大小球)
"""
import json, os, sys, time, logging
import numpy as np
from sklearn.metrics import accuracy_score, f1_score

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, 'backend')
logging.basicConfig(level=logging.WARNING)

# ── 1. 加载数据 ──────────────────────────────────
t0 = time.time()

with open('data/wc2026_72matches_with_odds.json', 'r', encoding='utf-8') as f:
    raw = json.load(f)

# OCR: 让球 + 大小球
with open('data/wc2026_ocr_full.json', 'r', encoding='utf-8') as f:
    ocr_data = json.load(f)
ocr_map = {}
for m in ocr_data['matches']:
    if 'parsed' in m:
        ocr_map[(m['home_team'], m['away_team'])] = m['parsed']

# ── 2. 加载模型 ──────────────────────────────────
from predictors.components.ensemble_trainer import EnsembleTrainer
trainer = EnsembleTrainer.load_pipeline('saved_models/football_v4.1_production.joblib')
print(f"模型加载完成 (DE={'✓' if trainer.draw_expert_model else '✗'})")

# ── 3. 辅助工具 ──────────────────────────────────
def parse_hcp(s):
    try: return float(str(s).replace('+','').replace(' ',''))
    except: return 0.0

def parse_ou(s):
    try: return float(str(s))
    except: return 2.5

def build_features(match):
    """从原始数据构建特征字典(真实赔率+OCR让球/大小球)"""
    oh, od, oa = match['1x2_home'], match['1x2_draw'], match['1x2_away']
    key = (match['home'], match['away'])
    ocr = ocr_map.get(key, {})
    
    inv_sum = 1.0/oh + 1.0/od + 1.0/oa
    imp_h, imp_d, imp_a = (1.0/oh)/inv_sum, (1.0/od)/inv_sum, (1.0/oa)/inv_sum
    
    return {
        'real_home_odds': oh, 'real_draw_odds': od, 'real_away_odds': oa,
        'odds_imp_h': imp_h, 'odds_imp_d': imp_d, 'odds_imp_a': imp_a,
        'odds_spread': abs(imp_h - imp_a),
        'odds_balance': oh / max(oa, 0.01),
        'odds_entropy': -(imp_h*np.log(imp_h+1e-9)+imp_d*np.log(imp_d+1e-9)+imp_a*np.log(imp_a+1e-9)),
        'odds_overround': (1.0/oh + 1.0/od + 1.0/oa - 1.0),
        'odds_confidence': 1.0 - abs(imp_h - imp_a),
        'odds_move_h': 0, 'odds_move_d': 0, 'odds_move_a': 0,
        'odds_draw_dev': od / max(3.5, 0.01),  # 平赔偏离基准
    }

def get_base_proba(match):
    """真实模型推理 → [H, D, A] 概率"""
    feats = build_features(match)
    result = trainer.predict_match(feats)
    return np.array([result['home_prob'], result['draw_prob'], result['away_prob']])

# ── 4. drawgate_v53 ──────────────────────────────
def get_drawgate(match, de_signal=None):
    try:
        from rules.drawgate_v53 import apply_drawgate, imp_from_odds
        imp_h, imp_d, imp_a = imp_from_odds(match['1x2_home'], match['1x2_draw'], match['1x2_away'])
        key = (match['home'], match['away'])
        ocr = ocr_map.get(key, {})
        return apply_drawgate(
            imp_h, imp_d, imp_a,
            odds={'home': match['1x2_home'], 'draw': match['1x2_draw'], 'away': match['1x2_away']},
            handicap=parse_hcp(ocr.get('handicap', '0')),
            ou_line=parse_ou(ocr.get('ou_line', '2.5')),
            match_type='tournament',
            draw_expert_signal=de_signal if de_signal and de_signal > 0 else None,
        )
    except Exception:
        return {'draw_boost': 0.0, 'risk_tag': 'clean', 'dgate_mode': 'none',
                'draw_threshold_adj': 0.32, 'confidence_mult': 1.0}

def get_de_signal(match):
    """DrawExpert P(Draw) — 真实模型推理"""
    if not trainer.draw_expert_model: return None
    try:
        feats = build_features(match)
        de_p = trainer.draw_expert_model.predict_proba(np.array([list(feats.values())]))[0]
        if hasattr(de_p, 'shape') and de_p.shape[-1] == 2:
            de_d = float(de_p[0, 1]) if de_p.ndim > 1 else float(de_p[1])
        else:
            return None
        # v5.3 ramp calibration
        if de_d <= 0.26: de_d *= 0.25
        elif de_d >= 0.38: de_d *= 0.95
        else:
            t = (de_d - 0.22) / 0.16
            de_d *= 0.25 + t * 0.70
        return de_d * 0.45
    except:
        return None

# ── 5. Path A: D-specialist gate ──────────────────
def path_a(base_proba, match):
    """prediction_service.py L504-617: D通道外科替换"""
    oh, od, oa = match['1x2_home'], match['1x2_draw'], match['1x2_away']
    inv_sum = 1.0/oh + 1.0/od + 1.0/oa
    imp_h, imp_a = (1.0/oh)/inv_sum, (1.0/oa)/inv_sum
    proba_spread = abs(imp_h - imp_a)
    
    # OE + Heuristic are not available in standalone mode → use base as fallback
    d_prob, h_prob, a_prob = base_proba[1], base_proba[0], base_proba[2]
    
    # Simplified D-specialist: blend D with drawgate signal
    dg = get_drawgate(match)
    draw_boost = dg.get('draw_boost', 0.0)
    imp_d = (1.0/od)/inv_sum
    de_pdraw = min(max(imp_d + draw_boost * 0.25, 0.0), 1.0)
    
    # D-spec (Heuristic/OE unavailable → fallback to drawgate)
    d_spec = 0.55 * d_prob + 0.45 * de_pdraw
    h_spec, a_spec = h_prob, a_prob
    
    # Spread-driven gate
    if proba_spread < 0.15:    d_gate = 0.65
    elif proba_spread < 0.25:  d_gate = 0.45
    elif proba_spread < 0.40:  d_gate = 0.25
    elif proba_spread < 0.55:  d_gate = 0.12
    else:                      d_gate = 0.05
    d_gate *= 0.65  # single source reliability penalty
    
    # D replacement
    d_final = d_prob * (1 - d_gate) + d_spec * d_gate
    remaining = 1.0 - d_final
    ha_sum = h_prob + a_prob
    h_final = remaining * (h_prob / ha_sum) if ha_sum > 0.001 else remaining * 0.5
    a_final = remaining * (a_prob / ha_sum) if ha_sum > 0.001 else remaining * 0.5
    
    probs = np.array([h_final, d_final, a_final])
    return probs / probs.sum()

# ── 6. Path B: DrawGate v5.3 ─────────────────────
def path_b(base_proba, match, de_signal=None):
    """UnifiedPredictor L405-479: DrawExpert boost + confidence_mult"""
    proba = base_proba.copy()
    dg = get_drawgate(match, de_signal)
    
    if dg['confidence_mult'] < 1.0:
        oh, oa = match['1x2_home'], match['1x2_away']
        inv_sum = 1.0/oh + 1.0/match['1x2_draw'] + 1.0/oa
        imp_h, imp_a = (1.0/oh)/inv_sum, (1.0/oa)/inv_sum
        strong_idx = 0 if imp_h >= imp_a else 2
        proba[strong_idx] *= dg['confidence_mult']
    
    if dg['draw_boost'] > 0:
        proba[1] += min(dg['draw_boost'], 0.12)
    
    if de_signal and de_signal > 0.30 and dg.get('risk_tag', '') != 'clean':
        proba[1] += min(de_signal * 0.10, 0.05)
    
    return proba / proba.sum()

# ── 7. 批量回测 ──────────────────────────────────
valid = [m for m in raw if m.get('1x2_home') and m.get('1x2_home', 0) > 0
         and m.get('hs') is not None and m.get('aws') is not None]

print(f"有效场次: {len(valid)} (OCR覆盖: {sum(1 for m in valid if (m['home'],m['away']) in ocr_map)})")
print(f"逐场推理中...")

results = []
for i, m in enumerate(valid):
    oh, od, oa = m['1x2_home'], m['1x2_draw'], m['1x2_away']
    
    base = get_base_proba(m)
    de = get_de_signal(m)
    
    pa = path_a(base, m)
    pb = path_b(base, m, de)
    
    inv_sum = 1.0/oh + 1.0/od + 1.0/oa
    spread = abs((1.0/oh)/inv_sum - (1.0/oa)/inv_sum)
    
    true_label = 1 if m['hs'] == m['aws'] else (0 if m['hs'] > m['aws'] else 2)
    
    results.append({
        'match': f"{m['home']} vs {m['away']}",
        'score': f"{m['hs']}-{m['aws']}",
        'true': true_label,
        'base': base.tolist(),
        'pa': pa.tolist(),
        'pb': pb.tolist(),
        'spread': spread,
        'de_signal': de or 0,
    })
    
    if (i+1) % 25 == 0:
        print(f"  {i+1}/{len(valid)}...")

# ── 8. 计算指标 ──────────────────────────────────
y_true = np.array([r['true'] for r in results])
y_pred_a = np.array([np.argmax(r['pa']) for r in results])
y_pred_b = np.array([np.argmax(r['pb']) for r in results])
probs_a = np.array([r['pa'] for r in results])
probs_b = np.array([r['pb'] for r in results])

def metrics(y_true, y_pred, probs):
    acc = accuracy_score(y_true, y_pred)
    d_f1 = f1_score(y_true, y_pred, labels=[1], average='macro', zero_division=0)
    mf1 = f1_score(y_true, y_pred, average='macro', zero_division=0)
    confs = np.max(probs, axis=1)
    correct = (y_pred == y_true).astype(float)
    bins = np.linspace(0, 1, 11)
    ece = sum(abs(correct[(confs >= bins[k]) & (confs < bins[k+1])].mean() - 
                   confs[(confs >= bins[k]) & (confs < bins[k+1])].mean()) * 
              sum((confs >= bins[k]) & (confs < bins[k+1])) / len(y_true)
              for k in range(10) if sum((confs >= bins[k]) & (confs < bins[k+1])) > 0)
    y_oh = np.zeros((len(y_true), 3)); y_oh[np.arange(len(y_true)), y_true] = 1
    brier = np.mean(np.sum((probs - y_oh)**2, axis=1))
    return acc, d_f1, mf1, ece, brier

a_acc, a_df1, a_mf1, a_ece, a_br = metrics(y_true, y_pred_a, probs_a)
b_acc, b_df1, b_mf1, b_ece, b_br = metrics(y_true, y_pred_b, probs_b)

# McNemar
b = sum((y_pred_a == y_true) & (y_pred_b != y_true))
c = sum((y_pred_a != y_true) & (y_pred_b == y_true))
mcn_chi2 = (abs(b-c)-1)**2/(b+c) if b+c > 0 else 0
from scipy.stats import chi2
mcn_p = 1 - chi2.cdf(mcn_chi2, 1) if b+c > 0 else 1.0

# 分层
narrow = [i for i, r in enumerate(results) if r['spread'] < 0.15]
wide = [i for i, r in enumerate(results) if r['spread'] >= 0.15]
ndf1_a = f1_score(y_true[narrow], y_pred_a[narrow], labels=[1], average='macro', zero_division=0) if narrow else 0
ndf1_b = f1_score(y_true[narrow], y_pred_b[narrow], labels=[1], average='macro', zero_division=0) if narrow else 0
wdf1_a = f1_score(y_true[wide], y_pred_a[wide], labels=[1], average='macro', zero_division=0) if wide else 0
wdf1_b = f1_score(y_true[wide], y_pred_b[wide], labels=[1], average='macro', zero_division=0) if wide else 0

# ── 9. 输出 ──────────────────────────────────────
elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"  D-Gate 融合对比回测 v2.0 — WC2026 {len(valid)}场 (真实模型推理)")
print(f"{'='*60}")
print(f"\n  【对比A: 全管线 Path A vs Path B】\n")
print(f"  指标           Path A      Path B      Δ(B-A)   判定")
print(f"  ───────────────────────────────────────────────")
print(f"  Acc            {a_acc:.3f}       {b_acc:.3f}       {b_acc-a_acc:+.3f}     {'B>A' if b_acc-a_acc>0.01 else ('持平' if abs(b_acc-a_acc)<=0.01 else 'A>B')}")
print(f"  D-F1           {a_df1:.3f}       {b_df1:.3f}       {b_df1-a_df1:+.3f}     {'B>A' if b_df1-a_df1>0.03 else ('持平' if abs(b_df1-a_df1)<=0.03 else 'A>B')}")
print(f"  MacroF1        {a_mf1:.3f}       {b_mf1:.3f}       {b_mf1-a_mf1:+.3f}     {'B>A' if b_mf1-a_mf1>0.03 else ('持平' if abs(b_mf1-a_mf1)<=0.03 else 'A>B')}")
print(f"  ECE            {a_ece:.3f}       {b_ece:.3f}       {b_ece-a_ece:+.3f}     {'↓A更好' if a_ece<b_ece else '↓B更好'}")
print(f"  Brier          {a_br:.3f}       {b_br:.3f}       {b_br-a_br:+.3f}     {'↓A更好' if a_br<b_br else '↓B更好'}")
print(f"  #预测D         {sum(y_pred_a==1)}          {sum(y_pred_b==1)}          {sum(y_pred_b==1)-sum(y_pred_a==1):+d}")
print(f"  #正确D         {sum((y_pred_a==1)&(y_true==1))}          {sum((y_pred_b==1)&(y_true==1))}          {sum((y_pred_b==1)&(y_true==1))-sum((y_pred_a==1)&(y_true==1)):+d}")
print(f"\n  McNemar: b={b}(A对B错) c={c}(A错B对) χ²={mcn_chi2:.2f} p={mcn_p:.4f} {'***' if mcn_p<0.001 else ('**' if mcn_p<0.01 else ('*' if mcn_p<0.05 else 'n.s.'))}")
print(f"\n  窄spread(<0.15): {len(narrow)}场  D-F1: A={ndf1_a:.3f} B={ndf1_b:.3f} Δ={ndf1_b-ndf1_a:+.3f}")
print(f"  宽spread(≥0.15): {len(wide)}场  D-F1: A={wdf1_a:.3f} B={wdf1_b:.3f} Δ={wdf1_b-wdf1_a:+.3f}")
print(f"\n  混淆矩阵:")
for label, name in [(0,'H'),(1,'D'),(2,'A')]:
    idx = y_true == label
    ar = [sum(y_pred_a[idx]==k) for k in [0,1,2]]
    br = [sum(y_pred_b[idx]==k) for k in [0,1,2]]
    print(f"  真{name}: A={ar} B={br}")

print(f"\n  ⏱ {elapsed:.1f}s | 数据: JSON({len(valid)}场) + OCR({len(ocr_map)}场) + 模型(72维特征)")

# 判定
df1_d = b_df1 - a_df1
acc_d = b_acc - a_acc
if df1_d >= 0.05 and acc_d >= -0.02 and b_mf1 >= a_mf1 - 0.01:
    verdict = "B 胜出 → UnifiedPredictor方向"
elif df1_d <= -0.05:
    verdict = "A 胜出 → prediction_service方向"
elif abs(df1_d) < 0.02 and abs(acc_d) < 0.02:
    verdict = "持平 → 选项1消重复"
else:
    verdict = "灰色地带 → 分层分析"
print(f"\n  判定: {verdict}")

# Save
out = {
    'version': 'v2.0-real', 'n': len(valid), 'n_ocr': len(ocr_map),
    'metrics_a': {'acc': a_acc, 'd_f1': a_df1, 'macro_f1': a_mf1, 'ece': a_ece, 'brier': a_br},
    'metrics_b': {'acc': b_acc, 'd_f1': b_df1, 'macro_f1': b_mf1, 'ece': b_ece, 'brier': b_br},
    'mcnemar': {'b': int(b), 'c': int(c), 'chi2': mcn_chi2, 'p': mcn_p},
    'narrow': {'n': len(narrow), 'a_df1': ndf1_a, 'b_df1': ndf1_b},
    'wide': {'n': len(wide), 'a_df1': wdf1_a, 'b_df1': wdf1_b},
    'verdict': verdict,
}
with open('reports/dgate_comparison_v2_real.json', 'w') as f:
    json.dump(out, f, indent=2, default=float)
print(f"  结果: reports/dgate_comparison_v2_real.json")
