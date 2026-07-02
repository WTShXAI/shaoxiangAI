#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
赔率深层信号挖掘 — 区分真假平局
===================================
计算7个赔率衍生信号，分析它们在真平局 vs 误判之间的分布
目标: 找到能减少误判且不牺牲平局召回的信号
"""
import sys, os, warnings
from pathlib import Path
from collections import defaultdict
import math
warnings.filterwarnings('ignore')

ARCH_ROOT = Path(r"D:/Architecture")
FAI_ROOT = Path(r"D:/AI/footballAI")

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

def analyze():
    features = []
    for m in MATCHES:
        home, away, oh, od, oa, hcp, ou, act, score, date = m
        
        # Basic implied probabilities
        total = 1/oh + 1/od + 1/oa
        imp_h = 1/oh/total
        imp_d = 1/od/total
        imp_a = 1/oa/total
        spread = abs(imp_h - imp_a)
        max_imp = max(imp_h, imp_a)
        fav_side = 'H' if imp_h > imp_a else 'A'
        fav_imp = max_imp
        dog_imp = min(imp_h, imp_a)
        
        # ═══ 7个深层赔率信号 ═══
        
        # S1: Draw odds相对便宜度 (d_odds / sqrt(h_odds * a_odds))
        # 低值 = draw赔率相对便宜 = 庄家认为平局概率高
        s1_draw_cheapness = od / math.sqrt(oh * oa)
        
        # S2: Overround分配 — draw份额
        # 高值 = 庄家在draw上收更多margin = 更确信不会平
        margin_h = 1/oh - imp_h/total
        margin_d = 1/od - imp_d/total
        margin_a = 1/oa - imp_a/total
        total_margin = margin_h + margin_d + margin_a
        s2_d_margin_share = margin_d / total_margin if total_margin > 0 else 0.33
        
        # S3: HCP偏离 — 实际让球 vs 赔率隐含期望让球
        # 正偏离 = 实际让球更深(比赔率暗示的还要看好强队) = 更不可能平
        if fav_side == 'H':
            expected_hcp = -math.log(max_imp / dog_imp) * 1.2
        else:
            expected_hcp = math.log(max_imp / dog_imp) * 1.2
        s3_hcp_deviation = abs(hcp) - abs(expected_hcp)
        
        # S4: 赔率结构对称度 (H和A赔率的接近程度)
        # 越接近 = 越均衡 = 更可能平
        s4_symmetry = 1.0 - min(oh, oa) / max(oh, oa)
        
        # S5: Draw赔率位置 — 在H/A赔率范围内的相对位置
        # draw赔率接近H赔率 = 庄家暗示平局和主胜概率接近
        s5_draw_position = (od - min(oh, oa)) / (max(oh, oa) - min(oh, oa)) if max(oh, oa) != min(oh, oa) else 1.0
        
        # S6: 水位差 — 赔率倒数差异 (隐含概率差异)
        s6_prob_gap = abs(1/oh - 1/oa)
        
        # S7: OU-让球错配 — OU线相对hcp深度
        # hcp深但ou低 = 保守预期(可能平) / hcp浅但ou高 = 开放比赛(可能不平)
        s7_ou_hcp_mismatch = ou / (abs(hcp) + 0.5)
        
        f = {
            'match': f'{home}vs{away}', 'date': date, 'score': score,
            'actual': act, 'oh': oh, 'od': od, 'oa': oa,
            'hcp': hcp, 'ou': ou,
            'imp_d': imp_d, 'spread': spread, 'max_imp': max_imp,
            's1': s1_draw_cheapness,
            's2': s2_d_margin_share,
            's3': s3_hcp_deviation,
            's4': s4_symmetry,
            's5': s5_draw_position,
            's6': s6_prob_gap,
            's7': s7_ou_hcp_mismatch,
        }
        features.append(f)
    
    # ═══ 分组分析 ═══
    draws = [f for f in features if f['actual'] == 'D']
    non_draws = [f for f in features if f['actual'] != 'D']
    
    # 用v4.1模型定义"被D-Gate误判的非平局"
    print("加载v4.1模型进行预测...")
    from predictors.unified_predictor import UnifiedPredictor
    model_path = str(FAI_ROOT / "saved_models" / "football_v4.1_production.joblib")
    up = UnifiedPredictor(model_path=model_path, enable_trap=False, enable_dh=False, use_threshold=False)
    
    # 先获取模型pD
    for f in features:
        try:
            r = up.predict(home=f['match'].split('vs')[0], away=f['match'].split('vs')[1],
                          odds_h=f['oh'], odds_d=f['od'], odds_a=f['oa'],
                          asian_handicap=f['hcp'], ou_line=f['ou'])
            probs = r.get('probabilities', {})
            f['model_pd'] = probs.get('D', f['imp_d'])
        except (KeyError, TypeError, AttributeError):
            f['model_pd'] = f['imp_d']
    
    # D-Gate v5.0 aggressive版判定
    def dgate_aggressive(f):
        pd = f['model_pd']
        spread = f['spread']
        max_imp = f['max_imp']
        imp_a = 1/f['oa'] / (1/f['oh']+1/f['od']+1/f['oa'])
        
        # Mode C
        if max_imp >= 0.70:
            d_boost = pd * 1.08
            if max_imp > 0.75 or abs(f['hcp']) >= 1.75:
                d_boost *= 2.2
            else:
                d_boost *= 1.8
            if f['od'] > 9.5 and f['ou'] >= 3.5 and abs(f['hcp']) >= 2.5:
                d_boost *= 0.3
            elif f['od'] > 9.5 and abs(f['hcp']) >= 2.5:
                d_boost *= 0.5
            if d_boost > 0.14:
                return True, 'C', d_boost
        
        # Mode C-away
        if imp_a > 0.65 and max_imp < 0.70:
            d_boost = pd * 1.08 * 2.0
            if d_boost > 0.14:
                return True, 'C-away', d_boost
        
        # Mode A
        if 0.48 <= max_imp <= 0.70:
            d_boost = pd * 1.08
            suppress = max(0.80, 1.0 - spread * 0.30)
            d_boost *= suppress
            if f['ou'] <= 2.5:
                d_boost *= 1.05
            if d_boost > 0.28:
                return True, 'A', d_boost
        
        # Mode B
        if spread < 0.15:
            d_boost = pd * 1.08 * 1.20
            if d_boost > 0.24:
                return True, 'B', d_boost
        
        # Default
        d_boost = pd * 1.08
        if spread > 0.40:
            d_boost *= 0.70
        elif spread > 0.20:
            d_boost *= 0.85
        if d_boost > 0.30:
            return True, 'default', d_boost
        
        return False, '-', 0
    
    for f in features:
        pred_d, mode, d_boost = dgate_aggressive(f)
        f['dg_pred_d'] = pred_d
        f['dg_mode'] = mode
        f['dg_boost'] = d_boost
    
    # 分组
    true_draws_detected = [f for f in features if f['actual'] == 'D' and f['dg_pred_d']]
    true_draws_missed = [f for f in features if f['actual'] == 'D' and not f['dg_pred_d']]
    false_alarms = [f for f in features if f['actual'] != 'D' and f['dg_pred_d']]
    correct_non_d = [f for f in features if f['actual'] != 'D' and not f['dg_pred_d']]
    
    # ═══ 信号分析 ═══
    print("=" * 100)
    print("📊 7个赔率深层信号分析 — 区分真平局 vs D-Gate误判")
    print("=" * 100)
    
    for sig_name, sig_key in [
        ('S1: Draw赔率便宜度(od/√oh·oa)', 's1'),
        ('S2: Draw overround份额', 's2'),
        ('S3: HCP偏离度', 's3'),
        ('S4: 赔率对称度', 's4'),
        ('S5: Draw赔率位置', 's5'),
        ('S6: 概率差 |1/H-1/A|', 's6'),
        ('S7: OU-HCP错配', 's7'),
    ]:
        draw_vals = [f[sig_key] for f in true_draws_detected]
        fp_vals = [f[sig_key] for f in false_alarms]
        
        import numpy as np
        d_mean = np.mean(draw_vals)
        fp_mean = np.mean(fp_vals)
        d_std = np.std(draw_vals)
        fp_std = np.std(fp_vals)
        
        # Cohen's d effect size
        pooled_std = math.sqrt((d_std**2 + fp_std**2) / 2) if (d_std + fp_std) > 0 else 1
        cohens_d = (d_mean - fp_mean) / pooled_std if pooled_std > 0 else 0
        
        # Overlap ratio (how much the distributions overlap)
        d_min, d_max = min(draw_vals), max(draw_vals)
        fp_min, fp_max = min(fp_vals), max(fp_vals)
        overlap_min = max(d_min, fp_min)
        overlap_max = min(d_max, fp_max)
        overlap_range = max(0, overlap_max - overlap_min)
        total_range = max(d_max, fp_max) - min(d_min, fp_min)
        overlap_pct = overlap_range / total_range * 100 if total_range > 0 else 100
        
        # If Cohen's d > 0.5, there's meaningful separation
        strength = '🟢 STRONG' if abs(cohens_d) > 0.8 else ('🟡 MODERATE' if abs(cohens_d) > 0.4 else '🔴 WEAK')
        
        print(f"\n{sig_name}")
        print(f"  真平局: μ={d_mean:.3f} σ={d_std:.3f} range=[{d_min:.3f}, {d_max:.3f}]")
        print(f"  误判:   μ={fp_mean:.3f} σ={fp_std:.3f} range=[{fp_min:.3f}, {fp_max:.3f}]")
        print(f"  Cohen's d={cohens_d:+.3f} 分布重叠={overlap_pct:.0f}% {strength}")
        
        # If strong, show which matches to filter
        if abs(cohens_d) > 0.5:
            # Find threshold that removes FPs without losing draws
            if d_mean > fp_mean:
                # Draws have higher value → set lower bound
                best_thresh = max(d_min, fp_max + (fp_max - fp_min) * 0.1)
                removed_fp = sum(1 for v in fp_vals if v < best_thresh)
                lost_draws = sum(1 for v in draw_vals if v < best_thresh)
            else:
                # Draws have lower value → set upper bound
                best_thresh = min(d_max, fp_min - (fp_min - fp_max) * 0.1)
                removed_fp = sum(1 for v in fp_vals if v > best_thresh)
                lost_draws = sum(1 for v in draw_vals if v > best_thresh)
            print(f"  建议阈值: {best_thresh:.3f} — 可移除{removed_fp}个误判, 牺牲{lost_draws}个平局")
    
    # ═══ 逐场详情 ═══
    print(f"\n{'='*100}")
    print(f"🎯 误判详情 — 与最相似的真平局对比")
    print(f"{'='*100}")
    
    for fp in false_alarms:
        # Find closest draw match (by odds similarity)
        best_draw = None
        best_dist = float('inf')
        for d in true_draws_detected:
            dist = abs(fp['oh']-d['oh']) + abs(fp['oa']-d['oa']) + abs(fp['hcp']-d['hcp'])
            if dist < best_dist:
                best_dist = dist
                best_draw = d
        
        if best_draw and best_dist < 1.0:  # Similar odds
            print(f"\n  ❌ {fp['match']} ({fp['score']}) ↔ ✅ {best_draw['match']} ({best_draw['score']})")
            print(f"     odds: {fp['oh']}/{fp['od']}/{fp['oa']} vs {best_draw['oh']}/{best_draw['od']}/{best_draw['oa']}")
            for sig_key, sig_name in [('s1','draw便宜度'),('s2','D-margin'),('s3','HCP偏离'),('s4','对称度')]:
                diff = fp[sig_key] - best_draw[sig_key]
                print(f"     {sig_name}: {fp[sig_key]:.3f} vs {best_draw[sig_key]:.3f} (Δ={diff:+.3f})")
    
    print(f"\n{'='*100}")
    print(f"📊 总结")
    print(f"{'='*100}")
    print(f"  真平局被检测: {len(true_draws_detected)}/{len(draws)} ({len(true_draws_detected)/len(draws):.0%})")
    print(f"  误判: {len(false_alarms)} 场")
    print(f"  正确非平局: {len(correct_non_d)} 场")
    
    return features

if __name__ == "__main__":
    features = analyze()
