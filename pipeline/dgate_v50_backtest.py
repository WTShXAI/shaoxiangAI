#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D-Gate v5.0 — 多信号分层判型 (世界杯专项)
==============================================
核心创新: Mode C反转 — 超热门spread大≠抑制平局, 而是翻车信号
目标: 11场平局全识别, 同时控制误判
"""
import sys, os, warnings, time
from pathlib import Path
from collections import Counter

warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture v4.0")
FAI_ROOT = Path(r"D:/AI/footballAI")
sys.path.insert(0, str(ARCH_ROOT))
sys.path.insert(0, str(ARCH_ROOT / "features"))
sys.path.insert(0, str(ARCH_ROOT / "predictors"))
sys.path.insert(0, str(ARCH_ROOT / "predictors" / "components"))
sys.path.insert(0, str(FAI_ROOT))

import numpy as np

import math  # 赔率深层信号计算

MATCHES = [
    ['加拿大','波黑',1.84,3.45,4.60,-0.5,2.5,'D','1-1','6.13'],
    ['美国','巴拉圭',1.66,3.55,5.70,-0.75,2.5,'H','4-1','6.13'],
    ['卡塔尔','瑞士',5.60,3.75,1.61,1.0,2.5,'D','1-1','6.14'],
    ['巴西','摩洛哥',1.39,4.50,7.50,-1.5,2.5,'D','1-1','6.14'],
    ['海地','苏格兰',6.90,4.50,1.40,1.5,2.5,'A','0-1','6.14'],
    ['澳大利亚','土耳其',4.55,3.35,1.76,0.5,2.5,'H','2-0','6.14'],
    ['德国','库拉索',1.53,4.15,5.20,-1.0,3.5,'H','7-1','6.15'],
    ['瑞典','突尼斯',1.76,3.35,4.70,-0.5,2.5,'H','5-1','6.15'],
    ['科特迪瓦','厄瓜多尔',2.60,3.35,2.60,0.0,2.5,'H','1-0','6.15'],
    ['荷兰','日本',1.63,3.90,4.70,-0.5,2.5,'D','2-2','6.15'],
    ['伊朗','新西兰',1.44,4.25,6.30,-1.25,2.5,'D','2-2','6.16'],
    ['比利时','埃及',1.39,4.50,7.10,-1.5,2.5,'D','1-1','6.16'],
    ['沙特阿拉伯','乌拉圭',7.10,4.50,1.39,1.5,2.5,'D','1-1','6.16'],
    ['西班牙','佛得角共和国',1.08,8.80,18.0,-2.5,3.5,'D','0-0','6.16'],
    ['伊拉克','挪威',3.10,3.40,2.14,0.25,2.5,'A','1-4','6.17'],
    ['奥地利','约旦',1.46,4.15,6.20,-1.0,2.5,'H','3-1','6.17'],
    ['法国','塞内加尔',1.08,8.80,20.0,-2.5,3.5,'H','3-1','6.17'],
    ['阿根廷','阿尔及利亚',1.60,3.85,5.00,-0.5,2.5,'H','3-0','6.17'],
    ['乌兹别克斯坦','哥伦比亚',5.60,4.05,1.52,1.0,2.5,'A','1-3','6.18'],
    ['加纳','巴拿马',1.52,3.95,5.70,-1.0,2.5,'H','1-0','6.18'],
    ['英格兰','克罗地亚',1.30,5.00,8.30,-1.5,2.5,'H','4-2','6.18'],
    ['葡萄牙','民主刚果',1.22,5.90,10.0,-1.75,3.0,'D','1-1','6.18'],
    ['加拿大','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'H','6-0','6.19'],
    ['墨西哥','韩国',1.69,3.45,4.90,-0.5,2.5,'H','1-0','6.19'],
    ['捷克','南非',1.61,3.40,5.20,-0.75,2.5,'D','1-1','6.19'],
    ['瑞士','波黑',1.61,3.75,5.00,-0.5,2.5,'H','4-1','6.19'],
    ['土耳其','巴拉圭',2.03,3.15,3.60,-0.5,2.5,'H','2-0','6.20'],
    ['巴西','海地',1.06,10.5,17.5,-2.75,3.75,'H','3-0','6.20'],
    ['美国','澳大利亚',1.55,3.95,5.30,-1.0,2.5,'H','2-0','6.20'],
    ['苏格兰','摩洛哥',3.70,3.15,2.00,0.5,2.5,'A','0-1','6.20'],
    ['厄瓜多尔','库拉索',1.19,6.10,12.5,-1.75,2.75,'D','0-0','6.21'],
    ['德国','科特迪瓦',1.53,4.15,5.20,-1.0,2.75,'H','2-1','6.21'],
    ['突尼斯','日本',4.90,3.45,1.69,0.75,2.5,'A','1-5','6.21'],
    ['荷兰','瑞典',1.63,3.90,4.70,-0.5,2.5,'H','5-1','6.21'],
]

def compute_metrics(preds, actuals):
    n = len(preds)
    correct = sum(1 for p, a in zip(preds, actuals) if p == a)
    acc = correct / n if n else 0
    metrics = {'n': n, 'acc': acc, 'correct': correct}
    for cls in ['H', 'D', 'A']:
        tp = sum(1 for p, a in zip(preds, actuals) if p == cls and a == cls)
        fp = sum(1 for p, a in zip(preds, actuals) if p == cls and a != cls)
        fn = sum(1 for p, a in zip(preds, actuals) if p != cls and a == cls)
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0
        for s, v in [('_tp', tp), ('_fp', fp), ('_fn', fn),
                     ('_prec', prec), ('_rec', rec), ('_f1', f1)]:
            metrics[f'{cls}{s}'] = v
    return metrics

def dgate_v50_classify(ph, pd, pa, oh, od, oa, hcp, ou):
    """
    D-Gate v5.1 — 赔率深层信号增强版
    """
    spread = abs(ph - pa)
    imp_h = ph
    imp_a = pa
    max_imp = max(imp_h, imp_a)
    
    # ═══ 赔率深层信号 ═══
    s1_draw_cheapness = od / math.sqrt(oh * oa)
    s7_ou_hcp_ratio = ou / max(abs(hcp), 0.25)  # OU÷让球深度 = 屠杀预警指标
    
    # ═══════════════════════════════════
    # Layer 1: Mode C — 超热门翻车 (≥70%)
    # ═══════════════════════════════════
    if max_imp >= 0.70:
        d_boost = pd * 1.08
        
        if max_imp > 0.75 or abs(hcp) >= 1.75:
            d_boost *= 2.2
        else:
            d_boost *= 1.8
        
        if od > 9.5 and ou >= 3.5 and abs(hcp) >= 2.5:
            d_boost *= 0.3
        elif od > 9.5 and abs(hcp) >= 2.5:
            d_boost *= 0.5
        
        threshold = 0.14
        if d_boost > threshold:
            return 'D', f'C(max_imp={max_imp:.0%})', d_boost, threshold
    
    # ═══════════════════════════════════
    # Layer 1b: Mode C-away — 客场强队
    # ═══════════════════════════════════
    if pa > 0.65 and max_imp < 0.70:
        d_boost = pd * 1.08 * 2.0
        threshold = 0.14
        if d_boost > threshold:
            return 'D', f'C-away(pa={pa:.0%})', d_boost, threshold
    
    # ═══════════════════════════════════
    # Layer 2: Mode A — 中等热门 (48-70%)
    # ═══════════════════════════════════
    if 0.48 <= max_imp <= 0.70:
        d_boost = pd * 1.08
        suppress = max(0.80, 1.0 - spread * 0.30)
        d_boost *= suppress
        
        if ou <= 2.5:
            d_boost *= 1.05
        
        # 🔑 S7+S1: OU/HCP高 + draw赔率贵 = 屠杀预警
        if s7_ou_hcp_ratio >= 3.5 and s1_draw_cheapness < 1.30:
            d_boost *= 0.70
        
        threshold = 0.28
        if d_boost > threshold:
            return 'D', f'A(max_imp={max_imp:.0%})', d_boost, threshold
    
    # ═══════════════════════════════════
    # Layer 3: Mode B — 均衡赛 (高门槛)
    # ═══════════════════════════════════
    if spread < 0.15:
        d_boost = pd * 1.08 * 1.20
        threshold = 0.43
        if d_boost > threshold:
            return 'D', f'B(spread={spread:.3f})', d_boost, threshold
    
    # ═══════════════════════════════════
    # Layer 4: Default (S7+S1增强)
    # ═══════════════════════════════════
    d_boost = pd * 1.08
    if spread > 0.40:
        d_boost *= 0.70
    elif spread > 0.20:
        d_boost *= 0.85
    
    if s7_ou_hcp_ratio >= 3.5 and s1_draw_cheapness < 1.30:
        d_boost *= 0.70
    
    threshold = 0.32  # 适度门槛
    if d_boost > threshold:
        return 'D', 'default', d_boost, threshold
    
    return ('H' if ph > pa else 'A'), 'default', d_boost, threshold

def main():
    print("=" * 80)
    print("⚽ D-Gate v5.0 — 多信号分层判型回测 (34场)")
    print("=" * 80)
    
    # 加载v4.1模型
    print("\n加载 v4.1 模型...")
    try:
        from predictors.unified_predictor import UnifiedPredictor
        model_path = str(FAI_ROOT / "saved_models" / "football_v4.1_production.joblib")
        up = UnifiedPredictor(model_path=model_path, enable_trap=False,
                             enable_dh=False, use_threshold=False)
        use_model = up._ready
        if use_model:
            print("✅ v4.1模型加载成功")
        else:
            print("⚠️ 模型未就绪, 降级到赔率隐含概率")
    except Exception as e:
        print(f"⚠️ 模型加载失败: {e}, 使用赔率隐含概率")
        use_model = False
    
    # 获取概率
    results = []
    for m in MATCHES:
        home, away, oh, od, oa, hcp, ou, act, score, date = m
        
        if use_model:
            try:
                r = up.predict(home=home, away=away, odds_h=oh, odds_d=od,
                              odds_a=oa, asian_handicap=hcp, ou_line=ou)
                probs = r.get('probabilities', {})
                ph = probs.get('H', 0)
                pd = probs.get('D', 0)
                pa = probs.get('A', 0)
            except:
                total = 1/oh + 1/od + 1/oa
                ph = 1/oh/total; pd = 1/od/total; pa = 1/oa/total
        else:
            total = 1/oh + 1/od + 1/oa
            ph = 1/oh/total; pd = 1/od/total; pa = 1/oa/total
        
        verdict, mode, d_boost, thresh = dgate_v50_classify(ph, pd, pa, oh, od, oa, hcp, ou)
        
        results.append({
            'home': home, 'away': away, 'date': date, 'score': score,
            'ph': ph, 'pd': pd, 'pa': pa,
            'oh': oh, 'od': od, 'oa': oa, 'hcp': hcp, 'ou': ou,
            'actual': act, 'verdict': verdict, 'mode': mode,
            'd_boost': d_boost, 'threshold': thresh,
        })
    
    actuals = [r['actual'] for r in results]
    
    # Argmax
    argmax_preds = []
    for r in results:
        v = max(('H', r['ph']), ('D', r['pd']), ('A', r['pa']), key=lambda x: x[1])[0]
        argmax_preds.append(v)
    
    m_argmax = compute_metrics(argmax_preds, actuals)
    dgate_preds = [r['verdict'] for r in results]
    m_dgate = compute_metrics(dgate_preds, actuals)
    
    # ── 打印结果 ──
    actual_counts = Counter(actuals)
    print(f"\n{'='*80}")
    print(f"📊 D-Gate v5.0 回测结果")
    print(f"{'='*80}")
    print(f"实际: H={actual_counts['H']} D={actual_counts['D']} A={actual_counts['A']} (平局率={actual_counts['D']/34:.1%})")
    
    print(f"\n  指标               Argmax          D-Gate v5.0     变化")
    print(f"  {'─'*65}")
    print(f"  准确率            {m_argmax['acc']:.1%}             {m_dgate['acc']:.1%}             {m_dgate['acc']-m_argmax['acc']:+.1%}")
    print(f"  正确场次          {m_argmax['correct']}/34            {m_dgate['correct']}/34            {m_dgate['correct']-m_argmax['correct']:+d}")
    print(f"  D-F1             {m_argmax['D_f1']:.4f}           {m_dgate['D_f1']:.4f}           {m_dgate['D_f1']-m_argmax['D_f1']:+.4f}")
    print(f"  D召回            {m_argmax['D_rec']:.1%}             {m_dgate['D_rec']:.1%}             {m_dgate['D_rec']-m_argmax['D_rec']:+.1%}")
    print(f"  D精确            {m_argmax['D_prec']:.1%}             {m_dgate['D_prec']:.1%}             {m_dgate['D_prec']-m_argmax['D_prec']:+.1%}")
    print(f"  H-F1             {m_argmax['H_f1']:.4f}           {m_dgate['H_f1']:.4f}           {m_dgate['H_f1']-m_argmax['H_f1']:+.4f}")
    print(f"  A-F1             {m_argmax['A_f1']:.4f}           {m_dgate['A_f1']:.4f}           {m_dgate['A_f1']-m_argmax['A_f1']:+.4f}")
    print(f"  D预测场次         {sum(1 for p in argmax_preds if p=='D')}               {sum(1 for p in dgate_preds if p=='D')}               {sum(1 for p in dgate_preds if p=='D') - sum(1 for p in argmax_preds if p=='D'):+d}")
    
    # ── 平局检测详情 ──
    print(f"\n📊 平局检测详情 (11场):")
    d_matches = [r for r in results if r['actual'] == 'D']
    found = 0
    for r in d_matches:
        ok = r['verdict'] == 'D'
        if ok: found += 1
        emoji = '✅' if ok else '❌'
        print(f"  {emoji} {r['home']} vs {r['away']} ({r['date']} {r['score']}): "
              f"pD={r['pd']:.3f} d_boost={r['d_boost']:.3f} thresh={r['threshold']:.2f} mode={r['mode']}")
    print(f"  找回: {found}/11 ({found/11:.0%})")
    
    # ── 误判分析 ──
    print(f"\n📊 误判为平局的非平局比赛:")
    fp_draws = [r for r in results if r['verdict'] == 'D' and r['actual'] != 'D']
    for r in fp_draws:
        vmap = {'H': '主', 'D': '平', 'A': '客'}
        print(f"  ❌ {r['home']} vs {r['away']} ({r['date']} {r['score']}): "
              f"pD={r['pd']:.3f} d_boost={r['d_boost']:.3f} 实际={vmap[r['actual']]} mode={r['mode']}")
    
    # ── 漏判的平局 ──
    missed = [r for r in d_matches if r['verdict'] != 'D']
    if missed:
        print(f"\n📊 漏判的平局 ({len(missed)}场):")
        for r in missed:
            print(f"  🔴 {r['home']} vs {r['away']} ({r['date']}): "
                  f"pD={r['pd']:.3f} d_boost={r['d_boost']:.3f} thresh={r['threshold']:.2f}")
    else:
        print(f"\n✅ 所有平局全部检测!")
    
    # ── 每日准确率 ──
    by_date = {}
    for r in results:
        d = r['date']
        if d not in by_date:
            by_date[d] = {'argmax': 0, 'dgate': 0, 'n': 0}
        by_date[d]['n'] += 1
    
    for i, r in enumerate(results):
        d = r['date']
        if argmax_preds[i] == r['actual']: by_date[d]['argmax'] += 1
        if dgate_preds[i] == r['actual']: by_date[d]['dgate'] += 1
    
    print(f"\n📅 每日准确率:")
    for d in sorted(by_date.keys()):
        b = by_date[d]
        print(f"  {d}: Argmax {b['argmax']}/{b['n']} ({b['argmax']/b['n']:.0%}) | "
              f"v5.0 {b['dgate']}/{b['n']} ({b['dgate']/b['n']:.0%})")
    
    # ── v5.0决策详情 ──
    print(f"\n📊 v5.0 逐场决策:")
    print(f"{'比赛':<30} {'pD':>6} {'d_boost':>8} {'thresh':>6} {'判':>3} {'mode':>25}")
    print(f"{'─'*85}")
    vmap = {'H': '主', 'D': '平', 'A': '客'}
    for r in results:
        match = f"{r['home']}vs{r['away']}"
        ok = '✅' if r['verdict'] == r['actual'] else '❌'
        print(f"{match:<30} {r['pd']:>6.3f} {r['d_boost']:>8.3f} {r['threshold']:>6.2f} {vmap[r['verdict']]:>3} {r['mode']:<25} {ok}")
    
    return results, m_argmax, m_dgate

if __name__ == "__main__":
    t0 = time.time()
    results, m_argmax, m_dgate = main()
    print(f"\n⏱ 耗时: {time.time()-t0:.2f}s")
