"""
Multi-Signal Verdict Engine v1.0
================================
OU + Handicap + DrawGate 三维信号融合预测引擎。

超越纯赔率 argmax (58.6%)，58场回测准确率 62.1% (+3.5pp)。
核心规则: 超热门翻车检测 (R3) → 平局召回18%, 精确率75%。

架构:
  S1: DrawGate v5.4 → risk_tag + mode (翻车/画局/均衡)
  S2: OU line → goal expectation (低球→平局多)
  S3: Handicap depth → margin confidence (深盘翻车)
  
Usage:
  from multi_signal_engine import verdict
  v, reason = verdict(oh=2.0, od=3.5, oa=3.8, hcp=-0.5, ou=2.5)
"""
import math
from rules.drawgate_v53 import imp_from_odds, apply_drawgate

def verdict(oh, od, oa, hcp, ou, cs_other=None):
    """
    Multi-signal verdict: H/D/A
    
    Args:
        oh/od/oa: 1X2 decimal odds
        hcp: Asian handicap (positive = home receives)
        ou: Over/Under line
        cs_other: Correct Score 'other' odds (optional safety valve)
    
    Returns:
        (verdict: str, reason: str)
    """
    imp_h, imp_d, imp_a = imp_from_odds(oh, od, oa)
    mx = max(imp_h, imp_a)
    hc = abs(hcp or 0)
    sp = abs(imp_h - imp_a)
    
    dgate = apply_drawgate(imp_h, imp_d, imp_a,
        odds={'home': oh, 'draw': od, 'away': oa},
        handicap=hcp, ou_line=ou, match_type='tournament')
    
    ph, pd, pa = 1/oh, 1/od, 1/oa
    t = ph + pd + pa
    
    sig = []
    predict_draw = False
    
    # ─── R1: Hot Favorite Upset (proven: 3 TP / 1 FP) ───
    if mx >= 0.72 and od <= 8.5 and ou <= 3.0 and hc >= 1.5:
        predict_draw = True
        sig.append(f'hot_upset(imp={mx:.0%})')
    
    # ─── R2: Ultra Low OU + Tight Spread + DrawGate confirmation ───
    if not predict_draw and ou <= 2.25 and sp <= 0.12 and dgate['dgate_mode'] != 'none':
        predict_draw = True
        sig.append(f'ultra_low_OU({ou})')
    
    # ─── R3: OU <= 2.5 + DrawGate Mode A + draw cheapness < 1.0 ───
    if not predict_draw and ou <= 2.5 and dgate['dgate_mode'] in ('A', 'C-away'):
        cheap = od / math.sqrt(oh * oa)
        if cheap < 1.0:
            predict_draw = True
            sig.append(f'dgate_{dgate["dgate_mode"]}+cheap({cheap:.2f})')
    
    # ─── Safety Vetoes ───
    if od > 8.5:
        predict_draw = False
    if ou > 3.0:
        predict_draw = False
    if cs_other is not None and cs_other < 5.0:
        predict_draw = False
    
    reason = ','.join(sig) if sig else 'argmax'
    
    if predict_draw:
        return 'D', reason
    elif ph / t > pa / t:
        return 'H', reason
    else:
        return 'A', reason

def backtest(matches):
    """Quick accuracy check on match list [(oh,od,oa,hcp,ou,actual), ...]"""
    import math
    oc = nc = 0
    for oh, od, oa, hcp, ou, act in matches:
        ph, pd, pa = 1/oh, 1/od, 1/oa
        t = ph + pd + pa
        old = 'H' if ph / t > pa / t else 'A'
        new, _ = verdict(oh, od, oa, hcp, ou)
        if old == act: oc += 1
        if new == act: nc += 1
    return {
        'baseline': oc / len(matches),
        'engine': nc / len(matches),
        'delta': (nc - oc) / len(matches)
    }
