#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D-Gate v5.1 端到端生产验证 + 36场未来预测
=============================================
1. 加载UnifiedPredictor v4.1 + D-Gate v5.1引擎
2. 对34场已完赛回测验证
3. 对36场未来比赛生成draw-risk预测
"""
import sys, os, json, math, warnings, time
from pathlib import Path
from collections import defaultdict

warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture")
FAI_ROOT = Path(r"D:/Architecture")

# 70场完整数据
# ═══════════════════════════════════════════════════
ALL_MATCHES = [
    # 6.13
    ['加拿大','波黑',1.84,3.45,4.60,-0.5,2.5,'D','1-1','6.13'],
    ['美国','巴拉圭',1.66,3.55,5.70,-0.75,2.5,'H','4-1','6.13'],
    # 6.14
    ['卡塔尔','瑞士',5.60,3.75,1.61,1.0,2.5,'D','1-1','6.14'],
    ['巴西','摩洛哥',1.39,4.50,7.50,-1.5,2.5,'D','1-1','6.14'],
    ['海地','苏格兰',6.90,4.50,1.40,1.5,2.5,'A','0-1','6.14'],
    ['澳大利亚','土耳其',4.55,3.35,1.76,0.5,2.5,'H','2-0','6.14'],
    # 6.15
    ['德国','库拉索',1.53,4.15,5.20,-1.0,3.5,'H','7-1','6.15'],
    ['瑞典','突尼斯',1.76,3.35,4.70,-0.5,2.5,'H','5-1','6.15'],
    ['科特迪瓦','厄瓜多尔',2.60,3.35,2.60,0.0,2.5,'H','1-0','6.15'],
    ['荷兰','日本',1.63,3.90,4.70,-0.5,2.5,'D','2-2','6.15'],
    # 6.16
    ['伊朗','新西兰',1.44,4.25,6.30,-1.25,2.5,'D','2-2','6.16'],
    ['比利时','埃及',1.39,4.50,7.10,-1.5,2.5,'D','1-1','6.16'],
    ['沙特阿拉伯','乌拉圭',7.10,4.50,1.39,1.5,2.5,'D','1-1','6.16'],
    ['西班牙','佛得角共和国',1.08,8.80,18.0,-2.5,3.5,'D','0-0','6.16'],
    # 6.17
    ['伊拉克','挪威',3.10,3.40,2.14,0.25,2.5,'A','1-4','6.17'],
    ['奥地利','约旦',1.46,4.15,6.20,-1.0,2.5,'H','3-1','6.17'],
    ['法国','塞内加尔',1.08,8.80,20.0,-2.5,3.5,'H','3-1','6.17'],
    ['阿根廷','阿尔及利亚',1.60,3.85,5.00,-0.5,2.5,'H','3-0','6.17'],
    # 6.18
    ['乌兹别克斯坦','哥伦比亚',5.60,4.05,1.52,1.0,2.5,'A','1-3','6.18'],
    ['加纳','巴拿马',1.52,3.95,5.70,-1.0,2.5,'H','1-0','6.18'],
    ['英格兰','克罗地亚',1.30,5.00,8.30,-1.5,2.5,'H','4-2','6.18'],
    ['葡萄牙','民主刚果',1.22,5.90,10.0,-1.75,3.0,'D','1-1','6.18'],
    # 6.19
    ['加拿大','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'H','6-0','6.19'],
    ['墨西哥','韩国',1.69,3.45,4.90,-0.5,2.5,'H','1-0','6.19'],
    ['捷克','南非',1.61,3.40,5.20,-0.75,2.5,'D','1-1','6.19'],
    ['瑞士','波黑',1.61,3.75,5.00,-0.5,2.5,'H','4-1','6.19'],
    # 6.20
    ['土耳其','巴拉圭',2.03,3.15,3.60,-0.5,2.5,'H','2-0','6.20'],
    ['巴西','海地',1.06,10.5,17.5,-2.75,3.75,'H','3-0','6.20'],
    ['美国','澳大利亚',1.55,3.95,5.30,-1.0,2.5,'H','2-0','6.20'],
    ['苏格兰','摩洛哥',3.70,3.15,2.00,0.5,2.5,'A','0-1','6.20'],
    # 6.21
    ['厄瓜多尔','库拉索',1.19,6.10,12.5,-1.75,2.75,'D','0-0','6.21'],
    ['德国','科特迪瓦',1.53,4.15,5.20,-1.0,2.75,'H','2-1','6.21'],
    ['突尼斯','日本',4.90,3.45,1.69,0.75,2.5,'A','1-5','6.21'],
    ['荷兰','瑞典',1.63,3.90,4.70,-0.5,2.5,'H','5-1','6.21'],
    # ═══ 6.22-6.28 未来36场 ═══
    ['乌拉圭','佛得角共和国',1.44,4.25,6.30,-1.25,2.5,'?','','6.22'],
    ['新西兰','埃及',4.55,3.35,1.76,0.75,2.5,'?','','6.22'],
    ['比利时','伊朗',1.39,4.50,7.10,-1.5,2.5,'?','','6.22'],
    ['西班牙','沙特阿拉伯',1.08,8.80,18.0,-2.5,3.5,'?','','6.22'],
    ['挪威','塞内加尔',2.14,3.40,3.10,0.0,2.5,'?','','6.23'],
    ['法国','伊拉克',1.08,8.80,20.0,-2.5,3.5,'?','','6.23'],
    ['约旦','阿尔及利亚',6.20,4.15,1.46,1.0,2.5,'?','','6.23'],
    ['阿根廷','奥地利',1.60,3.85,5.00,-0.5,2.5,'?','','6.23'],
    ['哥伦比亚','民主刚果',1.44,4.05,6.90,-1.0,2.5,'?','','6.24'],
    ['巴拿马','克罗地亚',5.70,3.95,1.52,1.0,2.5,'?','','6.24'],
    ['英格兰','加纳',1.30,5.00,8.30,-1.5,2.5,'?','','6.24'],
    ['葡萄牙','乌兹别克斯坦',1.22,5.90,10.0,-1.75,3.0,'?','','6.24'],
    ['南非','韩国',4.95,3.80,1.61,0.75,2.5,'?','','6.25'],
    ['捷克','墨西哥',4.25,3.35,1.81,-0.5,2.5,'?','','6.25'],
    ['摩洛哥','海地',1.34,4.85,7.50,-1.5,2.75,'?','','6.25'],
    ['波黑','卡塔尔',1.61,3.75,5.00,-0.5,2.5,'?','','6.25'],
    ['瑞士','加拿大',2.12,3.25,3.25,-0.25,2.25,'?','','6.25'],
    ['苏格兰','巴西',6.90,4.50,1.40,1.5,2.5,'?','','6.25'],
    ['厄瓜多尔','德国',4.45,3.55,1.72,-0.75,2.5,'?','','6.26'],
    ['土耳其','美国',2.60,3.50,2.41,0.0,2.5,'?','','6.26'],
    ['巴拉圭','澳大利亚',2.09,3.20,3.40,-0.25,2.25,'?','','6.26'],
    ['库拉索','科特迪瓦',11.0,5.80,1.21,-1.75,2.75,'?','','6.26'],
    ['日本','瑞典',2.11,3.30,3.25,-0.25,2.5,'?','','6.26'],
    ['突尼斯','荷兰',5.80,4.15,1.49,-1.0,2.5,'?','','6.26'],
    ['乌拉圭','西班牙',4.70,3.90,1.63,-0.75,2.5,'?','','6.27'],
    ['佛得角共和国','沙特阿拉伯',2.47,3.35,2.62,0.0,2.25,'?','','6.27'],
    ['埃及','伊朗',2.16,3.00,3.40,-0.25,2.0,'?','','6.27'],
    ['塞内加尔','伊拉克',1.40,4.40,7.00,-1.25,2.5,'?','','6.27'],
    ['挪威','法国',4.05,3.55,1.80,-0.75,2.5,'?','','6.27'],
    ['新西兰','比利时',9.00,5.20,1.28,-1.5,2.75,'?','','6.27'],
    ['克罗地亚','加纳',1.62,3.75,5.00,-0.75,2.5,'?','','6.28'],
    ['哥伦比亚','葡萄牙',3.25,3.30,2.11,-0.25,2.25,'?','','6.28'],
    ['巴拿马','英格兰',9.10,5.40,1.27,-1.5,2.75,'?','','6.28'],
    ['民主刚果','乌兹别克斯坦',2.27,3.25,2.97,0.0,2.25,'?','','6.28'],
    ['约旦','阿根廷',12.0,6.30,1.18,-2.0,3.0,'?','','6.28'],
    ['阿尔及利亚','奥地利',3.30,3.25,2.11,-0.25,2.25,'?','','6.28'],
]

def main():
    print("=" * 90)
    print("⚽ D-Gate v5.1 端到端生产验证 + 36场未来预测")
    print("=" * 90)
    
    # ── 加载模型和D-Gate引擎 ──
    print("\n[1/4] 加载v4.1模型...")
    from predictors.unified_predictor import UnifiedPredictor
    model_path = str(FAI_ROOT / "saved_models" / "football_v4.1_production.joblib")
    up = UnifiedPredictor(model_path=model_path, enable_trap=False, enable_dh=False, use_threshold=False)
    print(f"  {'✅' if up._ready else '⚠️'} 模型状态")
    
    print("\n[2/4] 加载D-Gate v5.1引擎...")
    try:
        from rules.d_gate_engine import apply_dgate_v51
        print("  ✅ v5.1引擎加载成功")
        engine_ready = True
    except Exception as e:
        print(f"  ❌ 引擎加载失败: {e}, 降级到内置逻辑")
        engine_ready = False
    
    # ── 对所有70场运行预测 ──
    print(f"\n[3/4] 运行 {len(ALL_MATCHES)} 场预测...")
    import numpy as np
    
    results = []
    for i, m in enumerate(ALL_MATCHES):
        home, away, oh, od, oa, hcp, ou, act, score, date = m
        is_completed = act != '?'
        
        # UnifiedPredictor预测
        try:
            r = up.predict(home=home, away=away, odds_h=oh, odds_d=od, odds_a=oa,
                          asian_handicap=hcp, ou_line=ou)
            probs = r.get('probabilities', {})
            ph = probs.get('H', 0)
            pd_model = probs.get('D', 0)
            pa = probs.get('A', 0)
        except:
            total = 1/oh + 1/od + 1/oa
            ph = 1/oh/total; pd_model = 1/od/total; pa = 1/oa/total
        
        # D-Gate v5.1判型
        if engine_ready:
            dg = apply_dgate_v51(
                imp_h=ph, imp_d=pd_model, imp_a=pa,
                odds={'home': oh, 'draw': od, 'away': oa},
                handicap=hcp, ou_line=ou,
                match_type='tournament',
            )
            verdict = dg['verdict']
            dg_mode = dg['d_gate_mode']
            dg_active = dg['d_gate_active']
            signals = dg.get('signals', [])
            s1 = dg.get('s1_draw_cheapness', 0)
            s7 = dg.get('s7_ou_hcp_ratio', 0)
        else:
            # Fallback
            spread = abs(ph - pa)
            max_imp = max(ph, pa)
            verdict = 'D' if (pd_model > 0.28 and spread < 0.25) else ('H' if ph > pa else 'A')
            dg_mode = 'fallback'
            dg_active = (verdict == 'D')
            signals = []
            s1 = s7 = 0
        
        results.append({
            'home': home, 'away': away, 'date': date, 'oh': oh, 'od': od, 'oa': oa,
            'ph': ph, 'pd': pd_model, 'pa': pa,
            'hcp': hcp, 'ou': ou,
            'actual': act, 'score': score,
            'verdict': verdict, 'dg_mode': dg_mode, 'dg_active': dg_active,
            'signals': signals, 's1': s1, 's7': s7,
            'completed': is_completed,
        })
        
        if (i + 1) % 10 == 0 or i == len(ALL_MATCHES) - 1:
            print(f"  ... {i+1}/{len(ALL_MATCHES)}")
    
    # ── 回测验证 (34场) ──
    completed = [r for r in results if r['completed']]
    future = [r for r in results if not r['completed']]
    
    print(f"\n{'='*90}")
    print(f"📊 回测验证 — 34场已完赛")
    print(f"{'='*90}")
    
    correct = sum(1 for r in completed if r['verdict'] == r['actual'])
    d_actual = [r for r in completed if r['actual'] == 'D']
    d_pred = [r for r in completed if r['verdict'] == 'D']
    d_correct = [r for r in d_pred if r['actual'] == 'D']
    d_missed = [r for r in d_actual if r['verdict'] != 'D']
    
    vmap = {'H':'主胜','D':'平局','A':'客胜'}
    print(f"  准确率: {correct}/34 = {correct/34:.1%}")
    print(f"  实际平局: {len(d_actual)}场 | 预测平局: {len(d_pred)}场")
    print(f"  命中平局: {len(d_correct)}场 | 漏判: {len(d_missed)}场")
    print(f"  D召回: {len(d_correct)/max(len(d_actual),1):.0%} | D精确: {len(d_correct)/max(len(d_pred),1):.0%}")
    
    if d_missed:
        print(f"\n  漏判平局:")
        for r in d_missed:
            print(f"    ❌ {r['home']} vs {r['away']} ({r['date']} {r['score']}): "
                  f"pD={r['pd']:.3f} 判决={vmap[r['verdict']]}")
    
    false_alarms = [r for r in completed if r['verdict'] == 'D' and r['actual'] != 'D']
    print(f"\n  误判({len(false_alarms)}场):")
    for r in false_alarms:
        print(f"    ❌ {r['home']} vs {r['away']} ({r['date']} {r['score']}): "
              f"pD={r['pd']:.3f} mode={r['dg_mode']}")
    
    # ── D-Gate决策分布 ──
    mode_counts = defaultdict(int)
    for r in completed:
        mode_counts[r['dg_mode']] += 1
    print(f"\n  D-Gate模式分布:")
    for mode in ['C', 'C-away', 'A', 'B', 'default', 'fallback']:
        if mode_counts.get(mode, 0) > 0:
            print(f"    Mode {mode}: {mode_counts[mode]}场")
    
    # ── 未来36场预测 ──
    print(f"\n{'='*90}")
    print(f"📅 未来36场预测 — D-Gate v5.1 draw-risk分析")
    print(f"{'='*90}")
    
    # 按日期分组
    by_date = defaultdict(list)
    for r in future:
        by_date[r['date']].append(r)
    
    draw_risk_high = []
    draw_risk_medium = []
    
    for date in sorted(by_date.keys()):
        matches = by_date[date]
        print(f"\n  ── {date} ({len(matches)}场) ──")
        for r in matches:
            tag = '🔴 DRAW' if r['verdict'] == 'D' else ('🟡 RISK' if r['dg_active'] else '  ')
            mode_str = f"[{r['dg_mode']}]" if r['dg_active'] else ''
            print(f"  {tag} {r['home']} vs {r['away']:<12} "
                  f"H={r['ph']:.1%} D={r['pd']:.1%} A={r['pa']:.1%} "
                  f"→ {vmap[r['verdict']]:<4} {mode_str}")
            
            if r['verdict'] == 'D':
                draw_risk_high.append(r)
            elif r['dg_active']:
                draw_risk_medium.append(r)
    
    # ── Draw risk汇总 ──
    print(f"\n{'='*90}")
    print(f"🔴 高风险平局预测 ({len(draw_risk_high)}场)")
    print(f"{'='*90}")
    for r in sorted(draw_risk_high, key=lambda x: x['date']):
        print(f"  {r['date']} {r['home']} vs {r['away']}: "
              f"pD={r['pd']:.1%} mode={r['dg_mode']} signals={r['signals']}")
    
    print(f"\n🟡 中风险 ({len(draw_risk_medium)}场)")
    for r in sorted(draw_risk_medium, key=lambda x: x['date']):
        print(f"  {r['date']} {r['home']} vs {r['away']}: "
              f"pD={r['pd']:.1%} mode={r['dg_mode']}")
    
    return results

if __name__ == "__main__":
    t0 = time.time()
    results = main()
    print(f"\n⏱ 总耗时: {time.time()-t0:.1f}s")
