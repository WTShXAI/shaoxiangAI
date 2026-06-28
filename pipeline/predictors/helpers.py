"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    np = None
    _HAS_NUMPY = False
    class _FakeArray:
        def __init__(self, data):
            self.data = list(data)
        def copy(self): return _FakeArray(self.data)
        def __iter__(self): return iter(self.data)
        def __getitem__(self, i): return self.data[i]
        def __len__(self): return len(self.data)
        def sum(self): return sum(self.data)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.data_classes import *  # noqa: F401, F403
from pipeline.predictors.ou_linkage import OULinkageEngine


# ═══ 多约束评分器 (WC2026真实数据驱动) ═══
# 原理: 综合方向+OU+历史频率+总进球分布, 给每个比分打分
# 公式: w = 方向匹配(硬约束) + OU匹配(0.3) + 比分频率(0.35) + 总球频率(0.2) + 方向先验(0.15)

import json
from pathlib import Path as _Path

def _load_score_freq():
    """加载WC2026全量64场比分频率数据"""
    try:
        p = _Path(__file__).parent.parent.parent / 'data' / 'score_freq_wc2026.json'
        if p.exists():
            return json.load(open(p, encoding='utf-8'))
    except:
        pass
    # 兜底: 内嵌频率表
    return {
        'freq': {'0-0':7,'1-1':6,'1-0':5,'0-1':4,'2-1':4,'3-1':4,'1-3':4,
                 '2-2':3,'3-0':3,'5-1':2,'1-4':2,'0-2':2,'2-0':2,'5-0':2,'0-3':2,'3-2':2},
        'total_goals': {0:7,1:9,2:10,3:10,4:13,5:7,6:7,8:1}
    }

_FREQ_CACHE = None

def _get_freq():
    global _FREQ_CACHE
    if _FREQ_CACHE is None:
        _FREQ_CACHE = _load_score_freq()
    return _FREQ_CACHE

def smart_score_rank(target_dir, target_ou_flag, ou_line, top_n=5):
    """多约束智能评分排序
    
    Args:
        target_dir: 'H' (主胜), 'A' (客胜), 'D' (平)
        target_ou_flag: 'O' (大球) or 'U' (小球)
        ou_line: 大小球盘口 (2.0/2.25/2.5/2.75/3.0)
        top_n: 返回前几个
    
    Returns:
        list of score strings ranked by combined weight
    """
    freq_data = _get_freq()
    freqs = freq_data['freq']
    goals = freq_data['total_goals']
    max_freq = max(freqs.values())
    max_goal = max(goals.values())
    
    ranked = []
    for h in range(7):
        for a in range(7):
            sc = f"{h}-{a}"
            # 硬约束: 方向匹配
            pd = 'H' if h>a else ('A' if a>h else 'D')
            if pd != target_dir:
                continue
            
            w = 0.0
            # 约束1: OU匹配 (0.3)
            total = h + a
            actual_ou = 'O' if total > ou_line else 'U'
            w += 0.30 if actual_ou == target_ou_flag else 0.0
            
            # 约束2: 比分频率 (0.35)
            w += 0.35 * (freqs.get(sc, 1) / max_freq)
            
            # 约束3: 总进球频率 (0.20)
            w += 0.20 * (goals.get(total, 1) / max_goal)
            
            # 约束4: 方向先验 (0.15) — H>D>A
            dir_prior = {'H': 0.47, 'A': 0.27, 'D': 0.27}
            w += 0.15 * dir_prior.get(pd, 0.25)
            
            ranked.append((round(w, 4), sc))
    
    ranked.sort(key=lambda x: -x[0])
    return [s for _, s in ranked[:top_n]]

def fault_tolerant_scores(target_dir, target_ou, ou_line, top_n=5):
    """容错多约束评分: best_score保持预测方向, alt_scores含反向容错
    
    返回结构: [best(预测方向), alt1(预测方向), alt2(反方向/平局容错), ...]
    
    策略:
    - best_score: 从预测方向硬约束中选Top-1 (保持主业)
    - alt_scores[0]: 从预测方向中选Top-2 (方向对但比分差一点)
    - alt_scores[1]: 从最可能的反向中选Top-1 (方向错时的容错)
    """
    # 预测方向Top-3
    primary_scores = smart_score_rank(target_dir, target_ou, ou_line, top_n=3)
    
    # 容错方向: 如果预测是H, 容错选D和A的Top-1
    other_dirs = [d for d in ['H', 'A', 'D'] if d != target_dir]
    contingency = []
    for d in other_dirs:
        scores = smart_score_rank(d, target_ou, ou_line, top_n=1)
        if scores:
            contingency.append(scores[0])
    
    # 组合: primary[0]为best, primary[1]为alt1, contingency[0]为alt2
    result = list(primary_scores[:2])
    # 加入最高频的反向比分作为容错
    if contingency:
        # 优先加入平局(如果预测不是平局), 因为平局是最常见的方向误判
        if 'D' in other_dirs:
            draw_scores = smart_score_rank('D', target_ou, ou_line, top_n=1)
            if draw_scores:
                result.append(draw_scores[0])
        else:
            result.append(contingency[0])
    
    return result[:5]

def _score_dir(sc):
    h,a = map(int, sc.split('-'))
    return 'H' if h>a else ('A' if a>h else 'D')

def triple_constraint_scores(match, hcp_outcome, direction, ou_dir):
    """涛哥三维约束模型: 让球结果 + 方向 + OU → 有效比分交集
    
    Args:
        match: MatchInput (需要hcp/ou_line)
        hcp_outcome: '让胜'/'让平'/'让负'
        direction: '胜'/'平'/'负' (1X2方向)
        ou_dir: '大'/'小'
    
    Returns:
        按总球数排序的有效比分列表
    """
    hcp = match.hcp
    ou_line = match.ou_line
    
    # 第一维: 让球约束
    s_hcp = set()
    for h in range(8):
        for a in range(8):
            if hcp > 0:      adjusted = h + hcp - a
            elif hcp < 0:    adjusted = h - a + hcp
            else:            adjusted = h - a
            
            if hcp_outcome == '让胜' and adjusted > 0:
                s_hcp.add(f'{h}-{a}')
            elif hcp_outcome == '让平' and adjusted == 0:
                s_hcp.add(f'{h}-{a}')
            elif hcp_outcome == '让负' and adjusted < 0:
                s_hcp.add(f'{h}-{a}')
    
    # 第二维: 方向约束
    s_dir = set()
    for h in range(8):
        for a in range(8):
            if direction == '胜' and h > a:
                s_dir.add(f'{h}-{a}')
            elif direction == '平' and h == a:
                s_dir.add(f'{h}-{a}')
            elif direction == '负' and a > h:
                s_dir.add(f'{h}-{a}')
    
    # 第三维: OU约束
    s_ou = set()
    for h in range(8):
        for a in range(8):
            total = h + a
            if ou_dir == '大' and total > ou_line:
                s_ou.add(f'{h}-{a}')
            elif ou_dir == '小' and total < ou_line:
                s_ou.add(f'{h}-{a}')
    
    # 交集
    result = s_hcp & s_dir & s_ou
    
    # 按总球排序
    scored = [(int(sc.split('-')[0]) + int(sc.split('-')[1]), sc) for sc in result]
    scored.sort()
    return [s for _, s in scored]


def _score_dir(sc):
    h,a = map(int, sc.split('-'))
    return 'H' if h>a else ('A' if a>h else 'D')

def _constrain_ou_to_line(ou_link: dict, match, form_result=None, silent: bool = False) -> dict:
    """
    P0-5: 外围OU盘口约束总进球 (何执策)
    
    问题: LINKAGE_MATRIX固定比分锚不考虑实际球队能力, 导致总进球偏离OU线
    审计: 6/22-24 中5/12场的总进球预测偏差>1.5球
    方案: OU线隐含市场总进球预期, 当预测偏离>1.5球时强制修正
    
    Args:
        ou_link: OU联动结果 (含scores)
        match: MatchInput
        form_result: Chain -1战绩数据(可选, 用于精细化)
    
    Returns:
        dict: {'adjusted': bool, 'scores': [...], 'reason': str}
    """
    import math as _math
    
    scores = ou_link.get('scores', [])
    if not scores:
        return {'adjusted': False, 'scores': scores}
    
    # 1. 获取外围OU线 (优先截图OCR数据 → 竞彩OU)
    ou_line = match.ou_line if hasattr(match, 'ou_line') else 2.5
    
    # 尝试加载截图OU数据
    try:
        import json
        from pathlib import Path
        ou_file = Path(__file__).parent.parent / 'data' / 'ou_screenshot_6_28.json'
        if ou_file.exists():
            with open(ou_file, 'r', encoding='utf-8') as f:
                screenshot_ou = json.load(f)
            match_key = f'{match.home}vs{match.away}'
            if match_key in screenshot_ou:
                ou_line = screenshot_ou[match_key]
                if not silent:
                    print(f"\n  [OU截图] {match_key}: 外围OU={ou_line} (竞彩={match.ou_line})")
    except (json.JSONDecodeError, FileNotFoundError) as e:
        logger.warning("加载截图OU数据失败: %s", e)
    
    # 2. 获取OU隐含总进球预期
    honesty = OULinkageEngine.get_ou_honesty(ou_line)
    expected_total = honesty.get('exp_goals', ou_line)
    honesty_mult = honesty.get('honesty_mult', 1.0)
    
    # 市场隐含λ = OU期望进球 (保守)
    market_lambda = expected_total * honesty_mult
    
    # 2. 计算当前预测的平均总进球
    totals = [int(s.split('-')[0]) + int(s.split('-')[1]) for s in scores]
    avg_total = sum(totals) / len(totals) if totals else 0
    
    # 3. 屠杀豁免: 屠杀λ重标定优先级 > OU约束
    if ou_link.get('massacre_rescaled'):
        if not silent:
            print(f"\n  [OU约束] ⚡ 屠杀λ重标定已生效, 跳过OU约束 (避免冲突)")
        return {'adjusted': False, 'scores': scores, 'reason': '屠杀λ重标定优先级>OU约束'}
    
    # 4. 判断是否需要修正 (偏差>1.2球)
    deviation = abs(avg_total - market_lambda)
    if deviation <= 1.2:
        return {'adjusted': False, 'scores': scores, 'reason': f'OU偏差{deviation:.1f}≤1.2, 无需修正'}
    
    # 5. 用OU隐含λ重新生成Poisson比分
    # λ分配: 根据双方10场GF比例
    lam_total = market_lambda
    if form_result and form_result.is_valid:
        h_gf = form_result.home.avg_gf or 1.0
        a_gf = form_result.away.avg_gf or 1.0
        lam_home = lam_total * h_gf / (h_gf + a_gf)
        lam_away = lam_total * a_gf / (h_gf + a_gf)
    else:
        # 无战绩数据: 默认55/45分配
        lam_home = lam_total * 0.55
        lam_away = lam_total * 0.45
    
    # 生成Poisson Top-8比分
    candidates = []
    for h in range(6):
        for a in range(6):
            try:
                ph = (_math.exp(-lam_home) * lam_home**h) / max(_math.factorial(h), 1)
                pa = (_math.exp(-lam_away) * lam_away**a) / max(_math.factorial(a), 1)
                candidates.append((ph * pa, f'{h}-{a}', h + a))
            except (OverflowError, ValueError):
                continue
    
    # 按概率排序, 取Top-5
    candidates.sort(key=lambda x: -x[0])
    constrained = [s for _, s, _ in candidates[:5]]
    
    if not silent:
        print(f"\n  [OU约束] 预测总球{avg_total:.1f} vs 市场{market_lambda:.1f} 偏差{deviation:.1f}→修正")
        print(f"    λ: home={lam_home:.2f} away={lam_away:.2f} | 修正比分: {constrained}")
    
    return {
        'adjusted': True,
        'scores': constrained,
        'reason': f'OU偏差{deviation:.1f}球→修正(λ: {lam_home:.1f}+{lam_away:.1f})',
        'lambda_home': lam_home,
        'lambda_away': lam_away,
    }

# ════════════════════════════════════════════════════
# 🔥 P0: 三路径对比投票 (v5.7 Agent思维设计)
# 路径A=模型v4.1 | 路径B=D-Gate规则 | 路径C=历史相似场
# 两条一致→采用; 全不一致→D-Gate>模型>历史
# ════════════════════════════════════════════════════

def _vote_three_paths(model_verdict: str, dgate_verdict: str, form_result,
                      match_home: str, match_away: str, hcp: float, ou_line: float):
    """三路径投票裁决 v5.7"""
    path_a = model_verdict  # v4.1模型
    path_b = dgate_verdict  # D-Gate规则
    path_c = '?'  # 历史相似场路径
    
    # 路径C: 基于战绩的方向
    if form_result and form_result.is_valid:
        gap = form_result.goal_diff_advantage
        if abs(gap) >= 2.0:
            path_c = 'H' if gap > 0 else 'A'
        elif abs(gap) >= 1.0:
            path_c = 'H' if gap > 0 else 'A'
        else:
            path_c = 'D'
    
    # 投票计数 (A weight=0.6, B weight=0.9, C weight=0.5)
    votes = {'H': 0, 'D': 0, 'A': 0}
    weight_a = 0.6; weight_b = 0.9; weight_c = 0.5
    
    for v, w in [(path_a, weight_a), (path_b, weight_b), (path_c, weight_c)]:
        if v in votes:
            votes[v] += w
    
    winner = max(votes, key=votes.get)
    consensus = sum(1 for v in [path_a, path_b, path_c] if v == winner)
    
    # 裁决逻辑
    if consensus >= 2:
        verdict = winner
        reason = f'三路径共识({consensus}/3): A={path_a} B={path_b} C={path_c}'
    elif path_b != winner:
        verdict = path_b  # D-Gate优先
        reason = f'D-Gate优先(分歧): A={path_a} B={path_b} C={path_c}'
    else:
        verdict = path_a
        reason = f'模型优先(分歧): A={path_a} B={path_b} C={path_c}'
    
    # 让2球场景历史相似场检索
    similar_match_ref = None
    if abs(hcp) >= 1.5:
        from rules.d_gate_utils import ALL_RESULTS
        for h, a, hg, ag, hcp_ref, _ in ALL_RESULTS:
            if abs(hcp_ref - abs(hcp)) <= 0.5 and abs((hg + ag) - ou_line) <= 1.0:
                similar_match_ref = f'{h}vs{a} {hg}-{ag}(hcp={hcp_ref})'
                break
    
    return {
        'verdict': verdict,
        'votes': votes,
        'consensus': f'{consensus}/3',
        'reason': reason,
        'paths': f'A(model)={path_a} B(D-Gate)={path_b} C(history)={path_c}',
        'similar_match': similar_match_ref,
    }

# ════════════════════════════════════════════════════
# 🔥 P0: 半场动态修正 (v5.7 Agent思维设计)
# 半场比分→下半场预测调整 (收手/拼命/巩固)
# ════════════════════════════════════════════════════

def _half_time_adjust(ht_home: int, ht_away: int, full_pred: dict, 
                      form_result, match_hcp: float) -> dict:
    """半场动态修正 v5.7
    输入: 半场比分, 全场预测, 战绩数据
    输出: 下半场预测调整 + 置信度衰减
    """
    ht_diff = ht_home - ht_away
    ht_total = ht_home + ht_away
    best_score = full_pred.get('best_score', '0-0')
    try:
        pred_h, pred_a = map(int, best_score.split('-'))
    except (ValueError, TypeError):
        pred_h, pred_a = 1, 1
    
    need_h = pred_h - ht_home
    need_a = pred_a - ht_away
    
    situation = 'unknown'
    confidence_decay = 0.0
    adj_notes = []
    
    if abs(ht_diff) >= 2:
        if form_result and form_result.is_valid:
            gap = form_result.goal_diff_advantage
            strong_leading = (gap > 0 and ht_diff > 0) or (gap < 0 and ht_diff < 0)
            if strong_leading:
                situation = '强队大幅领先'
                confidence_decay = 0.35
                adj_notes.append('强队下半场可能收手, 走水概率上升')
                need_h = max(need_h, 0)
                need_a = max(need_a, 0)
                adj_notes.append(f'下半场预期: {need_h}:{need_a}')
            else:
                situation = '弱队意外领先'
                confidence_decay = 0.50
                adj_notes.append('弱队领先不可持续, 强队追分概率高')
                need_h = max(need_h, 0) + (1 if gap > 0 else 0)
                need_a = max(need_a, 0) + (0 if gap > 0 else 1)
                adj_notes.append(f'下半场预期(追分修正): {need_h}:{need_a}')
    elif abs(ht_diff) <= 1:
        if ht_total == 0:
            situation = '半场沉闷'
            confidence_decay = 0.20
            adj_notes.append('0-0半场, 下半场突然爆发概率上升')
        else:
            situation = '半场胶着'
            confidence_decay = 0.10
            adj_notes.append('比分接近, 下半场方向不变')
    
    if abs(match_hcp) >= 1.5:
        adj_notes.append(f'深盘{match_hcp:+.1f}球半场修正: 让球方需净胜{max(match_hcp - ht_diff, 0):.1f}球才能穿盘')
        confidence_decay += 0.05
    
    return {
        'situation': situation,
        'ht_score': f'{ht_home}-{ht_away}',
        'confidence_decay': min(confidence_decay, 0.6),
        'need_second_half': f'{need_h}:{need_a}',
        'notes': adj_notes,
    }
