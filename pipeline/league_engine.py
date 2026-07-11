"""
哨响AI v7.1 — 五大联赛专用预测引擎
=====================================
针对英超/西甲/德甲/意甲/法甲的联赛特征优化。

与世界杯引擎的差异:
  - 大样本可靠: 近10场数据可靠, 战绩权重更高
  - 主场优势: 联赛主场胜率55-60%, 世界杯~35%
  - 平局率低: 联赛~25%, 世界杯淘汰赛31-38%
  - 无生存战: 无单场生死, 动机来自积分形势(争冠/保级/划水)
  - 市场有效: 联赛赔率更精准, 减少规则干预

流水线:
  输入端 → [1]赔率解析 → [2]战绩分析 → [3]动机分析(积分)
       → [4]主场加权 → [5]赔率高效区间 → [6]最终决策 → 输出端
"""

import os, sys, math
from typing import Dict
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# ═══ 复用 wc_engine 的基础函数 (战绩/赔率解析通用) ═══
from pipeline.wc_engine import (
    MatchInput, PipelineResult, parse_odds, analyze_form,
)


# ═══════════════════════════════════════════════════════════
# Step 3: 联赛动机分析 (替代世界杯的死球/生死战)
# ═══════════════════════════════════════════════════════════

def analyze_league_motivation(form: dict, odds: dict) -> dict:
    """
    联赛情境分析 — 基于战绩+赔率推导动机
    
    联赛关键信号:
      - title_race: 争冠队 vs 中下游 — 争冠方+0.10信度
      - relegation: 保级队 vs 中游 — 保级方+0.05信度  
      - mid_table: 双方无欲无求 — -0.10信度, 倾向平局
      - derby: 德比战 — 平局率+10%
    """
    motivation = {
        'home_motivation': 'neutral',   # title / relegation / mid_table / neutral
        'away_motivation': 'neutral',
        'derby': False,
        'dead_rubber': False,           # 双方均无欲无求
    }
    
    # 动机推导: 凭战绩净胜差+赔率差距估算
    net_diff = form.get('net_diff', 0)
    odds_gap = abs(odds['imp_h'] - odds['imp_a'])
    strength_gap = form.get('strength_gap', 'even')
    
    # 强队在主场且有较大优势 → 可能争冠/争欧战
    if strength_gap in ('massacre_home', 'edge_home') and net_diff > 1.0 and odds_gap > 0.30:
        motivation['home_motivation'] = 'title'
    # 强队在客场大优 → 客队争冠
    elif strength_gap in ('massacre_away', 'edge_away') and net_diff < -1.0 and odds_gap > 0.30:
        motivation['away_motivation'] = 'title'
    # 弱队在主场有微弱优势 → 可能保级战
    elif strength_gap == 'even' and abs(net_diff) < 0.5 and odds_gap < 0.10:
        motivation['dead_rubber'] = True  # 势均力敌的中游队
    # 弱队在主场受让 → 保级
    elif odds.get('market') == 'A' and strength_gap in ('edge_away',):
        motivation['home_motivation'] = 'relegation'
    
    return motivation


# ═══════════════════════════════════════════════════════════
# Step 4: 主场加权
# ═══════════════════════════════════════════════════════════

def home_advantage_weighting(odds: dict, form: dict, mot: dict) -> dict:
    """
    联赛主场优势: 55-60%主胜率 vs 世界杯~35%
    
    加权规则:
      - 中高区间(55-68%) + 主队 → +0.08 隐含概率
      - 中低区间(45-55%) + 主队 → +0.05
      - 德比 → 主场优势减半
      - 主队保级 → +0.03 额外加成
    """
    max_imp = max(odds['imp_h'], odds['imp_d'], odds['imp_a'])
    hcp = odds['imp_h']  # home chance probability
    
    boost = 0.0
    
    # 中高区间: 市场已有倾向, 主场再确认
    if 0.55 < max_imp <= 0.68 and odds['market'] == 'H':
        boost = 0.08
    # 中低区间: 微弱主场加成
    elif 0.45 < max_imp <= 0.55 and odds['imp_h'] > odds['imp_a']:
        boost = 0.05
    # 弱势区间的主队: 谨慎加成
    elif max_imp <= 0.45 and odds['imp_h'] > odds['imp_a']:
        boost = 0.03
    
    # 德比减半
    if mot.get('derby'):
        boost *= 0.5
    
    # 保级队额外加成
    if mot.get('home_motivation') == 'relegation':
        boost += 0.03
    
    return {
        'boost': boost,
        'adjusted_hcp': min(hcp + boost, 0.85),
        'reason': f'主场加权+{boost:.2f}' if boost > 0.01 else '无显著主场优势',
    }


# ═══════════════════════════════════════════════════════════
# Step 5: 联赛赔率高效区间 (信任市场)
# ═══════════════════════════════════════════════════════════

def league_zone_decision(odds: dict, form: dict, mot: dict, home_adv: dict) -> dict:
    """
    联赛决策 — 市场信任度高于世界杯
    
    区间策略:
      strong (>70%): 跟市场, 85%可靠
      mid_high (55-70%): 市场+主场加权, 70%可靠
      mid_low (45-55%): 谨慎, 市场方向+平局检测
      weak (<45%): 保守跟市场
    """
    max_imp = max(odds['imp_h'], odds['imp_d'], odds['imp_a'])
    zone = odds['zone']
    
    # 屠杀优先 (与世界杯一致)
    if form['massacre_triggered']:
        pred = 'H' if form['net_diff'] > 0 else 'A'
        return {'prediction': pred, 'confidence': 0.80, 'rationale': '屠杀预警', 'level': 'high'}
    
    # 强热区间: 联赛市场最可靠
    if zone == 'strong' or max_imp > 0.70:
        pred = odds['market']
        return {'prediction': pred, 'confidence': 0.85, 
                'rationale': f'强热方({max_imp*100:.0f}%), 联赛市场可靠', 'level': 'high'}
    
    # 中高区间: 市场+主场
    if zone == 'mid_safe' or max_imp > 0.55:
        pred = odds['market']
        conf = 0.70 + home_adv['boost']  # 主场加成
        # 主场队+中高区间+实力优势 → 提升信度
        if pred == 'H' and form['strength_gap'] in ('edge_home', 'massacre_home'):
            conf = min(conf + 0.05, 0.82)
        return {'prediction': pred, 'confidence': min(conf, 0.85),
                'rationale': f'中高区间+{home_adv["reason"]}', 'level': 'medium'}
    
    # 中低区间: 平局检测点 (联赛平局率~25%, 不强制)
    if zone == 'mid_danger' or max_imp > 0.45:
        # 均势无动机 → 倾向平局
        if mot.get('dead_rubber'):
            return {'prediction': 'D', 'confidence': 0.42, 
                    'rationale': '中低+无欲无求, 平局倾向', 'level': 'low'}
        # 有实力差 → 信任
        if form['strength_gap'] not in ('even',):
            pred = 'H' if 'home' in form['strength_gap'] else ('A' if 'away' in form['strength_gap'] else odds['market'])
            return {'prediction': pred, 'confidence': 0.55, 
                    'rationale': f'中低+{form["strength_gap"]}', 'level': 'medium'}
        # 市场指向 + 主场
        if odds['market'] == 'H' and home_adv['boost'] > 0.02:
            return {'prediction': 'H', 'confidence': 0.50,
                    'rationale': '中低+市场+主场', 'level': 'low'}
        # 默认: 跟市场, 低信度
        return {'prediction': odds['market'], 'confidence': 0.45,
                'rationale': '中低区间, 保守', 'level': 'low'}
    
    # 弱势区间
    pred = odds['market']
    # 保级队 vs 弱势客队 → 主队加成
    if pred == 'H' and mot.get('home_motivation') == 'relegation':
        return {'prediction': 'H', 'confidence': 0.50, 'rationale': '弱势+保级动力', 'level': 'low'}
    return {'prediction': pred, 'confidence': 0.45, 'rationale': '弱势区间, 市场指向', 'level': 'low'}


# ═══════════════════════════════════════════════════════════
# 主流水线
# ═══════════════════════════════════════════════════════════

def predict(match: MatchInput) -> PipelineResult:
    """联赛预测流水线"""
    # Step 1: 赔率解析 (与世界杯通用)
    odds = parse_odds(match.odds_h, match.odds_d, match.odds_a)
    
    # Step 2: 战绩分析 (与世界杯通用)
    form = analyze_form(match.home, match.away)
    
    # Step 3: 联赛动机分析 (联赛专属)
    mot = analyze_league_motivation(form, odds)
    
    # Step 4: 主场加权 (联赛专属)
    home_adv = home_advantage_weighting(odds, form, mot)
    
    # Step 5-6: 区间决策 + 比分
    decision = league_zone_decision(odds, form, mot, home_adv)
    
    # 死球降级 (双方均无动力)
    if mot.get('dead_rubber'):
        decision['confidence'] *= 0.75
        decision['rationale'] += '; 无欲无求降级'
        if decision['level'] == 'high':
            decision['level'] = 'medium'
    
    # 比分预测 (联赛版: 更保守的比分)
    pred = decision['prediction']
    if pred == 'H':
        if form['massacre_triggered'] and form['net_diff'] > 0:
            d = max(int(abs(form['net_diff']) + 1), 2)
            best_score = f"{min(d, 5)}-{max(int(d*0.3), 0)}"
        elif home_adv['boost'] > 0.05:
            best_score = "2-1"  # 主场优势, 对手可能进球
            alt_scores = ["2-0", "1-0"]
        else:
            best_score = "2-0"
            alt_scores = ["1-0", "2-1"]
    elif pred == 'A':
        if form['massacre_triggered'] and form['net_diff'] < 0:
            d = max(int(abs(form['net_diff']) + 1), 2)
            best_score = f"{max(int(d*0.3), 0)}-{min(d, 5)}"
        else:
            best_score = "1-2"  # 联赛客胜常伴随失球
            alt_scores = ["0-1", "0-2"]
    else:
        best_score = "1-1" if not form.get('weak_attack') else "0-0"
        alt_scores = ["0-0", "2-1"] if not form.get('weak_attack') else ["1-1"]
    
    return PipelineResult(
        prediction=pred,
        confidence=min(decision['confidence'], 0.95),
        best_score=best_score,
        alt_scores=alt_scores,
        market_baseline=odds['market'],
        market_probs={'H': round(odds['imp_h'],4), 'D': round(odds['imp_d'],4), 'A': round(odds['imp_a'],4)},
        mid_range_filtered=(odds['zone'] in ('mid_safe','mid_danger')),
        mispricing_overlay=False,
        massacre_triggered=form['massacre_triggered'],
        survival_clash=False,  # 联赛无生死战
        rationale=decision['rationale'],
        confidence_level=decision['level'],
    )
