#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
2026世界杯34场回测 — 真实v4.1模型
================================
加载 football_v4.1_production.joblib 对已完赛34场做预测
"""
import sys, os, warnings, time
from pathlib import Path
from collections import Counter
warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture")
FAI_ROOT = Path(r"D:/AI/footballAI")

import numpy as np

# 34场赔率数据
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
        for suffix, val in [('_tp', tp), ('_fp', fp), ('_fn', fn),
                            ('_prec', prec), ('_rec', rec), ('_f1', f1)]:
            metrics[f'{cls}{suffix}'] = val
    return metrics

def main():
    print("⚽ 2026世界杯34场回测 — v4.1模型")
    print("=" * 70)
    
    # 测试模型是否能加载
    model_path = FAI_ROOT / "saved_models" / "football_v4.1_production.joblib"
    print(f"模型路径: {model_path}")
    print(f"模型存在: {model_path.exists()}")
    
    if not model_path.exists():
        print("❌ 模型文件不存在! 使用备用方案")
        # 搜索模型
        import glob
        candidates = list((FAI_ROOT / "saved_models").glob("*production*"))
        candidates += list((ARCH_ROOT / "predictors").glob("*.joblib"))
        print(f"候选模型: {candidates}")
        if candidates:
            model_path = candidates[0]
            print(f"使用: {model_path}")
    
    try:
        print("\n加载 UnifiedPredictor...")
        from predictors.unified_predictor import UnifiedPredictor
        
        up = UnifiedPredictor(
            model_path=str(model_path),
            enable_trap=False,
            enable_dh=False,
            use_threshold=False,
        )
        
        if not up._ready:
            print("⚠️ UnifiedPredictor 未就绪, 降级到 argmax")
            raise RuntimeError("未就绪")
        
        print("✅ 模型加载成功")
        
        # 运行所有34场预测
        results = []
        for m in MATCHES:
            home, away, oh, od, oa, hc, ou, act, score, date = m
            try:
                r = up.predict(
                    home=home, away=away,
                    odds_h=oh, odds_d=od, odds_a=oa,
                    asian_handicap=hc,
                    ou_line=ou,
                )
                probs = r.get('probabilities', {})
                ph = probs.get('H', 0)
                pd = probs.get('D', 0)
                pa = probs.get('A', 0)
                
                # 提取D-Gate相关信号
                d_signal = r.get('draw_signal', 0) if isinstance(r, dict) else 0
                
                results.append({
                    'home': home, 'away': away, 'date': date,
                    'ph': ph, 'pd': pd, 'pa': pa,
                    'draw_signal': d_signal,
                    'actual': act, 'score': score,
                })
            except Exception as e:
                print(f"  ❌ {home} vs {away}: {e}")
                # Fallback to argmax on implied prob
                total = 1/oh + 1/od + 1/oa
                ph = 1/oh/total; pd = 1/od/total; pa = 1/oa/total
                results.append({
                    'home': home, 'away': away, 'date': date,
                    'ph': ph, 'pd': pd, 'pa': pa,
                    'draw_signal': 0,
                    'actual': act, 'score': score,
                    'error': str(e)[:50],
                })
        
        # Armax判定
        argmax_preds = []
        for r in results:
            v = max(('H', r['ph']), ('D', r['pd']), ('A', r['pa']), key=lambda x: x[1])[0]
            argmax_preds.append(v)
        
        # D-Gate判决 (阈值0.28, 世界杯)
        dgate_preds = []
        for r in results:
            ph, pd, pa = r['ph'], r['pd'], r['pa']
            spread = abs(ph - pa)
            d_adj = pd * 1.08  # 世界杯全局加成
            
            if ph > 0.72 and pd < 0.20 and r.get('draw_signal', 0) > 0:
                d_adj *= 1.40  # 超热门翻车
            if spread < 0.15:
                d_adj *= 1.12  # 均衡赛
            
            threshold = 0.28
            if d_adj > threshold:
                dgate_preds.append('D')
            else:
                dgate_preds.append('H' if ph > pa else 'A')
        
        actuals = [r['actual'] for r in results]
        m_argmax = compute_metrics(argmax_preds, actuals)
        m_dgate = compute_metrics(dgate_preds, actuals)
        
        print(f"\n{'='*70}")
        print(f"📊 v4.1模型回测结果 — 34场")
        print(f"{'='*70}")
        
        a_counts = Counter(actuals)
        print(f"实际分布: H={a_counts['H']} D={a_counts['D']} A={a_counts['A']} (D={a_counts['D']/34:.1%})")
        print(f"\n模型平均输出: pH={np.mean([r['ph'] for r in results]):.3f} pD={np.mean([r['pd'] for r in results]):.3f} pA={np.mean([r['pa'] for r in results]):.3f}")
        
        print(f"\n指标              Argmax          D-Gate")
        print(f"{'─'*50}")
        print(f"准确率            {m_argmax['acc']:.1%}            {m_dgate['acc']:.1%}")
        print(f"正确场次          {m_argmax['correct']}/34           {m_dgate['correct']}/34")
        print(f"D-F1            {m_argmax['D_f1']:.4f}          {m_dgate['D_f1']:.4f}")
        print(f"D召回            {m_argmax['D_rec']:.1%}            {m_dgate['D_rec']:.1%}")
        print(f"D精确            {m_argmax['D_prec']:.1%}            {m_dgate['D_prec']:.1%}")
        print(f"H-F1            {m_argmax['H_f1']:.4f}          {m_dgate['H_f1']:.4f}")
        print(f"A-F1            {m_argmax['A_f1']:.4f}          {m_dgate['A_f1']:.4f}")
        
        # 显示模型pD vs 实际
        print(f"\n📊 平局分析:")
        for r in results:
            if r['actual'] == 'D':
                arg = 'D' if max(('H', r['ph']), ('D', r['pd']), ('A', r['pa']), key=lambda x: x[1])[0] == 'D' else '✗'
                dg = 'D' if dgate_preds[results.index(r)] == 'D' else '✗'
                print(f"  {r['home']} vs {r['away']} ({r['date']} {r['score']}): "
                      f"pD={r['pd']:.3f} argmax={arg} dgate={dg}")
        
        # 逐场pD排序(所有比赛)
        print(f"\n📊 pD排序 (所有34场):")
        sorted_r = sorted(results, key=lambda x: x['pd'], reverse=True)
        vmap = {'H': '主', 'D': '平', 'A': '客'}
        for r in sorted_r:
            is_draw = r['actual'] == 'D'
            marker = '🔴' if is_draw else '⚪'
            print(f"  {marker} {r['home']} vs {r['away']} ({r['date']}): "
                  f"pD={r['pd']:.3f} pH={r['ph']:.3f} pA={r['pa']:.3f} "
                  f"实际={vmap[r['actual']]} ({r['score']})")
        
        return results, m_argmax, m_dgate
        
    except Exception as e:
        import traceback
        print(f"❌ 模型加载/预测失败: {e}")
        traceback.print_exc()
        return None, None, None

if __name__ == "__main__":
    t0 = time.time()
    results, _, _ = main()
    if results:
        print(f"\n⏱ 耗时: {time.time()-t0:.2f}s")
