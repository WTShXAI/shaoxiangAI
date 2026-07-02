"""
D-Gate 融合管线对比回测 — v2.0
================================
Path A (prediction_service.py D-specialist gate) vs Path B (UnifiedPredictor DrawGate v5.3)
70场 WC2026 完赛数据 + 两层对比(全管线 + DrawGate消融)
"""
import json, os, sys, time, logging
import numpy as np
from collections import defaultdict
from sklearn.metrics import accuracy_score, f1_score, classification_report, brier_score_loss

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, 'backend')
logging.basicConfig(level=logging.WARNING)

# ── 1. 数据加载 ──────────────────────────────────
with open('data/wc2026_72matches_with_odds.json', 'r') as f:
    raw = json.load(f)

matches = []
for m in raw:
    oh, od_, oa = m.get('1x2_home'), m.get('1x2_draw'), m.get('1x2_away')
    hs, aws = m.get('hs'), m.get('aws')
    if not (oh and od_ and oa and oh > 0 and od_ > 0 and oa > 0): continue
    if hs is None or aws is None: continue
    handicap = 0.85 * (oh - oa) / (oh + oa) * 2  # 估算公式
    match = {
        'home': m['home'], 'away': m['away'],
        'oh': oh, 'od': od_, 'oa': oa,
        'hs': hs, 'aws': aws,
        'handicap': round(handicap, 2),
        'ou_line': 2.5,
        'date': m.get('date', ''),
    }
    # stage: JSON无此字段, 暂时统一为tournament
    match['stage'] = 'tournament'
    matches.append(match)

N = len(matches)
print(f"\n{'='*60}\n  D-Gate 融合对比回测 — WC2026 {N}场\n{'='*60}")

# ── 2. 模型加载 ──────────────────────────────────
from predictors.components.ensemble_trainer import EnsembleTrainer
trainer = EnsembleTrainer.load_pipeline('saved_models/football_v4.1_production.joblib')
print(f"  EnsembleTrainer v4.1 加载完成 (DrawExpert={'✓' if trainer.draw_expert_model else '✗'})")

# ── 2b. 真实生产路径: PredictionService.predict_single ──
# 验证改动1(窄spread D干预)+改动2(NameError修复)在生产代码中的真实效果,
# 而非干净室重实现(path_a_predict)的模拟结果.
from services.prediction_service import PredictionService
_prod_svc = PredictionService()
print(f"  PredictionService 加载完成 (真实生产路径)")

# ── 3. 辅助: 获取基础模型proba ────────────────────
def get_base_proba(match):
    """从模型获取原始三分类概率(不经过任何D-Gate)"""
    from pipeline.predictors.data_classes import MatchInput
    mi = MatchInput(
        home=match['home'], away=match['away'],
        odds_h=match['oh'], odds_d=match['od'], odds_a=match['oa'],
        hcp=match['handicap'], ou_line=match['ou_line'],
        matchday=3, stage=match['stage']
    )
    try:
        result = trainer.predict_single(mi)
        h, d, a = result['probabilities']['home'], result['probabilities']['draw'], result['probabilities']['away']
        return np.array([h, d, a])
    except Exception:
        # Fallback: use odds implied
        inv = np.array([1/m['oh'], 1/m['od'], 1/m['oa']])
        return inv / inv.sum()

# ── 4. 辅助: Heuristic + OE ──────────────────────
try:
    from agents.heuristic_predictor import HeuristicPredictor
    hpred = HeuristicPredictor()
    _HEUR_AVAIL = True
except Exception:
    hpred = None; _HEUR_AVAIL = False

def get_heuristic_proba(match):
    """获取HeuristicPredictor输出(Path A依赖)"""
    if not _HEUR_AVAIL: return None
    try:
        from pipeline.predictors.data_classes import MatchInput
        mi = MatchInput(
            home=match['home'], away=match['away'],
            odds_h=match['oh'], odds_d=match['od'], odds_a=match['oa'],
            hcp=match['handicap'], ou_line=match['ou_line'],
            matchday=3, stage=match['stage']
        )
        proba = hpred.predict_proba(mi)[0]
        return np.array([float(proba[0]), float(proba[1]), float(proba[2])])
    except: return None

def get_oe_proba():
    """获取OE (Order Ensemble) 子模型输出"""
    try:
        oe = trainer.get_oe_output()
        if oe: return np.array([oe['home'], oe['draw'], oe['away']])
    except: pass
    return None

def get_de_signal(match):
    """获取DrawExpert P(Draw)"""
    try:
        if not trainer.draw_expert_model: return None
        from pipeline.predictors.data_classes import MatchInput
        mi = MatchInput(
            home=match['home'], away=match['away'],
            odds_h=match['oh'], odds_d=match['od'], odds_a=match['oa'],
            hcp=match['handicap'], ou_line=match['ou_line'],
            matchday=3, stage=match['stage']
        )
        X = trainer._build_features(mi)
        de_p = trainer.draw_expert_model.predict_proba(X.reshape(1, -1))
        if de_p.shape[1] == 2:
            de_d = float(de_p[0, 1])
            # v5.3 linear ramp calibration
            if de_d <= 0.26: de_d *= 0.25
            elif de_d >= 0.38: de_d *= 0.95
            else:
                t = (de_d - 0.22) / 0.16
                de_d *= 0.25 + t * 0.70
            de_d *= 0.45  # de_mult
            return de_d
    except: pass
    return None

# ── 5. drawgate_v53 ──────────────────────────────
def get_drawgate(match, de_signal=None):
    """获取DrawGate v5.3输出"""
    try:
        from rules.drawgate_v53 import apply_drawgate, imp_from_odds
        imp_h, imp_d, imp_a = imp_from_odds(match['oh'], match['od'], match['oa'])
        return apply_drawgate(
            imp_h, imp_d, imp_a,
            odds={'home': match['oh'], 'draw': match['od'], 'away': match['oa']},
            handicap=match['handicap'], ou_line=match['ou_line'],
            match_type='tournament',
            draw_expert_signal=de_signal if de_signal and de_signal > 0 else None,
        )
    except Exception as e:
        return {'draw_boost': 0.0, 'risk_tag': 'clean', 'dgate_mode': 'none', 
                'draw_threshold_adj': 0.32, 'confidence_mult': 1.0}

# ── 6. Path A: D-specialist gate ──────────────────
def path_a_predict(match, base_proba, h_proba=None, oe_proba=None, de_signal=None):
    """
    prediction_service.py D-Gate Fusion:
    D通道外科替换 — D_final = D_meta*(1-d_gate) + D_spec*d_gate
    """
    d_prob = base_proba[1]
    h_prob, a_prob = base_proba[0], base_proba[2]

    # Heuristic
    d_heur = h_proba[1] if h_proba is not None else d_prob
    h_heur = h_proba[0] if h_proba is not None else h_prob
    a_heur = h_proba[2] if h_proba is not None else a_prob

    # OE
    d_oe = oe_proba[1] if oe_proba is not None else None
    if oe_proba is not None:
        oe_entropy = abs(max(oe_proba) - min(oe_proba))
        if oe_entropy < 0.02:
            d_oe = None

    # DrawExpert
    de_pdraw = de_signal
    if de_pdraw is None and match['oh'] > 0:
        dg = get_drawgate(match)
        draw_boost = dg.get('draw_boost', 0.0)
        imp_d = 1.0 / match['od'] / (1.0 / match['oh'] + 1.0 / match['od'] + 1.0 / match['oa'])
        de_pdraw = min(max(imp_d + draw_boost * 0.25, 0.0), 1.0)

    # D-specialist
    if d_oe is not None and de_pdraw is not None:
        d_spec = 0.40 * d_heur + 0.30 * d_oe + 0.30 * de_pdraw
        h_spec = 0.55 * h_heur + 0.45 * oe_proba[0]
        a_spec = 0.55 * a_heur + 0.45 * oe_proba[2]
    elif d_oe is not None:
        d_spec = 0.55 * d_heur + 0.45 * d_oe
        h_spec = 0.55 * h_heur + 0.45 * oe_proba[0]
        a_spec = 0.55 * a_heur + 0.45 * oe_proba[2]
    elif de_pdraw is not None:
        d_spec = 0.55 * d_heur + 0.45 * de_pdraw
        h_spec = h_heur; a_spec = a_heur
    else:
        d_spec = d_heur; h_spec = h_heur; a_spec = a_heur

    # D-gate (spread-driven)
    imp_h = 1.0 / match['oh'] / (1.0 / match['oh'] + 1.0 / match['od'] + 1.0 / match['oa'])
    imp_a = 1.0 / match['oa'] / (1.0 / match['oh'] + 1.0 / match['od'] + 1.0 / match['oa'])
    proba_spread = abs(imp_h - imp_a)

    if proba_spread < 0.15:    d_gate = 0.65
    elif proba_spread < 0.25:  d_gate = 0.45
    elif proba_spread < 0.40:  d_gate = 0.25
    elif proba_spread < 0.55:  d_gate = 0.12
    else:                      d_gate = 0.05

    # D-signal agreement modulation
    if d_oe is not None:
        d_agreement = 1.0 - abs(d_oe - d_heur) / max(d_oe + d_heur, 0.001)
        d_gate *= (0.5 + 0.5 * d_agreement)
    else:
        d_gate *= 0.65

    # D channel replacement
    d_final = d_prob * (1 - d_gate) + d_spec * d_gate
    remaining = 1.0 - d_final
    ha_sum = h_prob + a_prob
    if ha_sum > 0.001:
        h_final = remaining * (h_prob / ha_sum)
        a_final = remaining * (a_prob / ha_sum)
    else:
        h_final = remaining * 0.5; a_final = remaining * 0.5

    probs = np.array([h_final, d_final, a_final])
    return probs / probs.sum()

# ── 7. Path B: DrawGate v5.3 ─────────────────────
def path_b_predict(match, base_proba, de_signal=None):
    """
    UnifiedPredictor DrawGate v5.3:
    DrawExpert boost + confidence_mult — 加法式抬D
    """
    proba = base_proba.copy()
    dg = get_drawgate(match, de_signal)

    # Confidence decay on strong team
    if dg['confidence_mult'] < 1.0:
        imp_h = 1.0 / match['oh'] / (1.0 / match['oh'] + 1.0 / match['od'] + 1.0 / match['oa'])
        imp_a = 1.0 / match['oa'] / (1.0 / match['oh'] + 1.0 / match['od'] + 1.0 / match['oa'])
        strong_idx = 0 if imp_h >= imp_a else 2
        proba[strong_idx] *= dg['confidence_mult']

    # DrawExpert boost
    if dg['draw_boost'] > 0:
        de_boost = min(dg['draw_boost'], 0.12)
        proba[1] += de_boost

    # DrawExpert signal auxiliary boost
    if de_signal and de_signal > 0.30 and dg.get('risk_tag', '') != 'clean':
        proba[1] += min(de_signal * 0.10, 0.05)

    return proba / proba.sum()

def path_b_no_drawgate(match, base_proba):
    """Path B without DrawGate (消融实验) — 仅base proba"""
    return base_proba.copy()

# ── 8. 批量回测 ──────────────────────────────────
print(f"\n  开始逐场推理 ({N}场)...")
results = []

for i, m in enumerate(matches):
    base_proba = get_base_proba(m)
    h_proba = get_heuristic_proba(m)
    oe_proba = get_oe_proba()
    de_signal = get_de_signal(m)

    pa = path_a_predict(m, base_proba, h_proba, oe_proba, de_signal)
    pb = path_b_predict(m, base_proba, de_signal)
    pb_no_dg = path_b_no_drawgate(m, base_proba)

    # 真实生产路径: 直接调 predict_single (验证改动1/2的真实效果, 非模拟)
    pa_prod = None
    try:
        r = _prod_svc.predict_single(
            m['home'], m['away'], league='世界杯',
            custom_odds={'home': m['oh'], 'draw': m['od'], 'away': m['oa'],
                         'asian_handicap': m['handicap'], 'ou_line': m['ou_line']}
        )
        if r is not None:
            pp = r['probabilities']
            pa_prod = np.array([pp['home'], pp['draw'], pp['away']])
    except Exception as _pe:
        pass  # 生产路径失败(冷启动/DB), 用 None 标记, 指标计算时跳过

    imp_h = 1.0 / m['oh'] / (1.0 / m['oh'] + 1.0 / m['od'] + 1.0 / m['oa'])
    imp_a = 1.0 / m['oa'] / (1.0 / m['oh'] + 1.0 / m['od'] + 1.0 / m['oa'])
    spread = abs(imp_h - imp_a)

    true_label = 1 if m['hs'] == m['aws'] else (0 if m['hs'] > m['aws'] else 2)

    rec = {
        'match': f"{m['home']} vs {m['away']}",
        'true': true_label,
        'base': base_proba.tolist(),
        'pa': pa.tolist(),
        'pb': pb.tolist(),
        'pb_no_dg': pb_no_dg.tolist(),
        'spread': spread,
        'stage': m['stage'],
    }
    if pa_prod is not None:
        rec['pa_prod'] = pa_prod.tolist()
    results.append(rec)

    if (i+1) % 20 == 0:
        print(f"    {i+1}/{N}...")

# ── 9. 指标计算 ──────────────────────────────────
print(f"\n{'='*60}\n  指标计算\n{'='*60}")

y_true = np.array([r['true'] for r in results])
y_pred_a = np.array([np.argmax(r['pa']) for r in results])
y_pred_b = np.array([np.argmax(r['pb']) for r in results])
y_pred_bnd = np.array([np.argmax(r['pb_no_dg']) for r in results])

def compute_metrics(y_true, y_pred, probs):
    acc = accuracy_score(y_true, y_pred)
    d_f1 = f1_score(y_true, y_pred, labels=[1], average='macro', zero_division=0)
    macro_f1 = f1_score(y_true, y_pred, average='macro', zero_division=0)
    # ECE 10-bin
    confs = np.max(probs, axis=1)
    correct = (y_pred == y_true).astype(float)
    bins = np.linspace(0, 1, 11)
    ece = 0.0
    for k in range(10):
        mask = (confs >= bins[k]) & (confs < bins[k+1])
        if mask.sum() > 0:
            ece += abs(correct[mask].mean() - confs[mask].mean()) * mask.sum() / len(y_true)
    # Brier
    y_onehot = np.zeros((len(y_true), 3))
    y_onehot[np.arange(len(y_true)), y_true] = 1
    brier = np.mean(np.sum((probs - y_onehot)**2, axis=1))
    return acc, d_f1, macro_f1, ece, brier

probs_a = np.array([r['pa'] for r in results])
probs_b = np.array([r['pb'] for r in results])
probs_bnd = np.array([r['pb_no_dg'] for r in results])

a_acc, a_df1, a_mf1, a_ece, a_brier = compute_metrics(y_true, y_pred_a, probs_a)
b_acc, b_df1, b_mf1, b_ece, b_brier = compute_metrics(y_true, y_pred_b, probs_b)
bnd_acc, bnd_df1, bnd_mf1, bnd_ece, bnd_brier = compute_metrics(y_true, y_pred_bnd, probs_bnd)

# ── 9b. 真实生产路径指标 (验证改动1/2, 仅在有 pa_prod 的子集上计算) ──
prod_idx = [i for i, r in enumerate(results) if 'pa_prod' in r]
ap_acc = ap_df1 = ap_mf1 = ap_ece = ap_brier = None
n_prod = len(prod_idx)
if n_prod > 0:
    yt_prod = y_true[prod_idx]
    probs_prod = np.array([results[i]['pa_prod'] for i in prod_idx])
    ypred_prod = np.argmax(probs_prod, axis=1)
    ap_acc, ap_df1, ap_mf1, ap_ece, ap_brier = compute_metrics(yt_prod, ypred_prod, probs_prod)

# ── 10. McNemar ──────────────────────────────────
def mcnemar_test(pred_a, pred_b, y_true):
    b = np.sum((pred_a == y_true) & (pred_b != y_true))  # A对B错
    c = np.sum((pred_a != y_true) & (pred_b == y_true))  # A错B对
    if b + c > 0:
        chi2 = (abs(b - c) - 1)**2 / (b + c)
        from scipy.stats import chi2 as chi2_dist
        p = 1 - chi2_dist.cdf(chi2, 1)
    else:
        chi2, p = 0, 1.0
    return chi2, p, b, c

mcn_chi2, mcn_p, mcn_b, mcn_c = mcnemar_test(y_pred_a, y_pred_b, y_true)

# ── 11. 分层: 窄spread ───────────────────────────
narrow_idx = [i for i, r in enumerate(results) if r['spread'] < 0.15]
wide_idx = [i for i, r in enumerate(results) if r['spread'] >= 0.15]

def subset_metrics(indices):
    if not indices: return 0, 0
    yt = y_true[indices]
    yp_a = y_pred_a[indices]; yp_b = y_pred_b[indices]
    return (accuracy_score(yt, yp_a), accuracy_score(yt, yp_b),
            f1_score(yt, yp_a, labels=[1], average='macro', zero_division=0),
            f1_score(yt, yp_b, labels=[1], average='macro', zero_division=0))

na_acc, nb_acc, na_df1, nb_df1 = subset_metrics(narrow_idx)
wa_acc, wb_acc, wa_df1, wb_df1 = subset_metrics(wide_idx)

# 窄spread子集生产路径D-F1 (验证改动1的核心指标)
nap_df1 = wap_df1 = None
if n_prod > 0:
    # 窄spread ∩ 有pa_prod的子集
    narrow_prod = [i for i in narrow_idx if 'pa_prod' in results[i]]
    wide_prod = [i for i in wide_idx if 'pa_prod' in results[i]]
    if narrow_prod:
        yt_np = y_true[narrow_prod]
        yp_np = np.argmax(np.array([results[i]['pa_prod'] for i in narrow_prod]), axis=1)
        nap_df1 = f1_score(yt_np, yp_np, labels=[1], average='macro', zero_division=0)
    if wide_prod:
        yt_wp = y_true[wide_prod]
        yp_wp = np.argmax(np.array([results[i]['pa_prod'] for i in wide_prod]), axis=1)
        wap_df1 = f1_score(yt_wp, yp_wp, labels=[1], average='macro', zero_division=0)

# ── 12. 输出 ─────────────────────────────────────
print(f"""
{'='*60}
  D-Gate 融合管线对比回测 — WC2026 {N}场
{'='*60}

  【对比A: 全管线 Path A vs Path B】

  指标               Path A          Path B          Δ(B-A)    判定
  ─────────────────────────────────────────────────────────────
  Acc               {a_acc:.3f}           {b_acc:.3f}           {b_acc-a_acc:+.3f}     {'B > A' if b_acc-a_acc > 0.01 else ('持平' if abs(b_acc-a_acc)<=0.01 else 'A > B')}
  D-F1              {a_df1:.3f}           {b_df1:.3f}           {b_df1-a_df1:+.3f}     {'B > A' if b_df1-a_df1 > 0.03 else ('持平' if abs(b_df1-a_df1)<=0.03 else 'A > B')}
  MacroF1           {a_mf1:.3f}           {b_mf1:.3f}           {b_mf1-a_mf1:+.3f}     {'B > A' if b_mf1-a_mf1 > 0.03 else ('持平' if abs(b_mf1-a_mf1)<=0.03 else 'A > B')}
  ECE               {a_ece:.3f}           {b_ece:.3f}           {b_ece-a_ece:+.3f}     {'↓B更好' if b_ece < a_ece else '↓A更好'}
  Brier             {a_brier:.3f}         {b_brier:.3f}         {b_brier-a_brier:+.3f} {'↓B更好' if b_brier < a_brier else '↓A更好'}
  #预测D           {sum(y_pred_a==1)}                    {sum(y_pred_b==1)}                    {sum(y_pred_b==1)-sum(y_pred_a==1):+d}
  #正确D           {sum((y_pred_a==1)&(y_true==1))}                   {sum((y_pred_b==1)&(y_true==1))}                   {sum((y_pred_b==1)&(y_true==1))-sum((y_pred_a==1)&(y_true==1)):+d}

  McNemar: b={mcn_b}(A对B错) c={mcn_c}(A错B对) χ²={mcn_chi2:.2f} p={mcn_p:.4f}
    显著性: {'*** p<0.001' if mcn_p<0.001 else ('** p<0.01' if mcn_p<0.01 else ('* p<0.05' if mcn_p<0.05 else 'n.s.'))}

  ─────────────────────────────────────────────────────────────
  窄spread (<0.15): {len(narrow_idx)}场
    Path A D-F1={na_df1:.3f}  Path B D-F1={nb_df1:.3f}  Δ={nb_df1-na_df1:+.3f}
    Path A(生产) D-F1={'N/A' if nap_df1 is None else f'{nap_df1:.3f}'}  ← 改动1干预效果
  宽spread (≥0.15): {len(wide_idx)}场
    Path A D-F1={wa_df1:.3f}  Path B D-F1={wb_df1:.3f}  Δ={wb_df1-wa_df1:+.3f}
    Path A(生产) D-F1={'N/A' if wap_df1 is None else f'{wap_df1:.3f}'}  ← 不应退化

  【对比B: DrawGate 消融】
                NO DrawGate      WITH DrawGate    Δ
  Acc           {bnd_acc:.3f}             {b_acc:.3f}             {b_acc-bnd_acc:+.3f}
  D-F1          {bnd_df1:.3f}             {b_df1:.3f}             {b_df1-bnd_df1:+.3f}
  MacroF1       {bnd_mf1:.3f}             {b_mf1:.3f}             {b_mf1-bnd_mf1:+.3f}

  【对比C: 真实生产路径 predict_single (验证改动1窄spread干预 + 改动2 NameError修复)】
  说明: 直接调真实 PredictionService.predict_single, 非干净室模拟.
        有效样本 {n_prod}/{N} 场 (生产路径返回非None的场次)
""")
if n_prod > 0:
    print(f"""  指标               Path A(模拟)    Path A(生产)    Path B          Δ(生产-A模拟)
  ─────────────────────────────────────────────────────────────
  Acc               {a_acc:.3f}           {ap_acc:.3f}           {b_acc:.3f}           {ap_acc-a_acc:+.3f}
  D-F1              {a_df1:.3f}           {ap_df1:.3f}           {b_df1:.3f}           {ap_df1-a_df1:+.3f}
  MacroF1           {a_mf1:.3f}           {ap_mf1:.3f}           {b_mf1:.3f}           {ap_mf1-a_mf1:+.3f}
  ECE               {a_ece:.3f}           {ap_ece:.3f}           {b_ece:.3f}           {ap_ece-a_ece:+.3f}
  Brier             {a_brier:.3f}         {ap_brier:.3f}         {b_brier:.3f}         {ap_brier-a_brier:+.3f}
""")
else:
    print("  ⚠ 生产路径全部返回None, 无法对比 (检查DB/模型加载)")

nap_str = f"{nap_df1:.3f}" if nap_df1 is not None else "N/A"
nap_tag = " ✅ >0 干预生效" if (nap_df1 is not None and nap_df1 > 0) else (" ❌ 仍=0 未脱离" if nap_df1 == 0 else "")
wap_str = f"{wap_df1:.3f}" if wap_df1 is not None else "N/A"
acc_tag = " ✅ >-0.03" if (ap_acc is not None and ap_acc - a_acc > -0.03) else ""
print(f"""
  ─────────────────────────────────────────────────────────────
  验收线 (改动1窄spread干预):
  • 窄spread子集生产D-F1 = {nap_str}{nap_tag}
  • 宽spread子集生产D-F1 = {wap_str} (不应显著退化)
  • 全量生产Acc Δ(vs A模拟) = {ap_acc-a_acc:+.3f}{acc_tag}
  • 全量生产D-F1 Δ(vs A模拟) = {ap_df1-a_df1:+.3f} (A模拟={a_df1:.3f}, B={b_df1:.3f})

{'='*60}
""")

# ── 13. 判定 ──────────────────────────────────────
df1_delta = b_df1 - a_df1
acc_delta = b_acc - a_acc
mcn_sig = mcn_p < 0.01

if df1_delta >= 0.05 and acc_delta >= -0.02 and mcn_sig and b_mf1 >= a_mf1 - 0.01 and b_ece <= a_ece + 0.03:
    verdict = "✅ Path B (UnifiedPredictor DrawGate v5.3) 显著胜出 → 选UnifiedPredictor方向, 推选项2合并"
elif df1_delta <= -0.05:
    verdict = "❌ Path A 反超 → 回退Path A, Path B需重新调参"
elif abs(df1_delta) < 0.02 and abs(acc_delta) < 0.02:
    verdict = "🟡 持平 → 维持现状, 先做选项1(消重复), 等淘汰赛全结束(N~104)再判"
else:
    verdict = "🟠 灰色地带 → 需分层分析定位问题子集"

print(f"  判定: {verdict}")

# ── 14. 混淆矩阵 ──────────────────────────────────
print(f"\n  混淆矩阵:")
print(f"  Path A              Path B")
for label, name in [(0,'H'),(1,'D'),(2,'A')]:
    idx = y_true == label
    a_row = [int(sum((y_pred_a[idx]==k))) for k in [0,1,2]]
    b_row = [int(sum((y_pred_b[idx]==k))) for k in [0,1,2]]
    print(f"  真{name}: {a_row}    {b_row}")

# 生产路径混淆矩阵 (验证改动1/2)
if n_prod > 0:
    print(f"\n  Path A(生产) 混淆矩阵 (n={n_prod}):")
    for label, name in [(0,'H'),(1,'D'),(2,'A')]:
        idx = y_true[prod_idx] == label
        ap_row = [int(sum((ypred_prod[idx]==k))) for k in [0,1,2]]
        print(f"  真{name}: {ap_row}")

# Save results
output = {
    'n_matches': N, 'timestamp': time.strftime('%Y-%m-%d %H:%M'),
    'verdict': verdict,
    'metrics_a': {'acc': a_acc, 'd_f1': a_df1, 'macro_f1': a_mf1, 'ece': a_ece, 'brier': a_brier},
    'metrics_b': {'acc': b_acc, 'd_f1': b_df1, 'macro_f1': b_mf1, 'ece': b_ece, 'brier': b_brier},
    'metrics_b_no_dg': {'acc': bnd_acc, 'd_f1': bnd_df1, 'macro_f1': bnd_mf1},
    'mcnemar': {'chi2': mcn_chi2, 'p': mcn_p, 'b': mcn_b, 'c': mcn_c},
    'narrow': {'n': len(narrow_idx), 'a_df1': na_df1, 'b_df1': nb_df1},
    'wide': {'n': len(wide_idx), 'a_df1': wa_df1, 'b_df1': wb_df1},
    'production': {
        'n_valid': n_prod,
        'metrics_a_prod': {'acc': ap_acc, 'd_f1': ap_df1, 'macro_f1': ap_mf1, 'ece': ap_ece, 'brier': ap_brier},
        'narrow': {'d_f1': nap_df1},
        'wide': {'d_f1': wap_df1},
    },
}
with open('reports/dgate_comparison_results.json', 'w') as f:
    json.dump(output, f, indent=2, default=float)
print(f"\n  结果已保存: reports/dgate_comparison_results.json")
