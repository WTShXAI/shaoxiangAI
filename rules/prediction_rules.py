"""
================================================================
footballAI — 预测规则引擎 v1.0 (从 SP/prediction_rules.py 移植)
                    规则参数化版本
================================================================

规则分层:
  Layer 1: 比赛类型规则 (先判断是什么比赛)
  Layer 2: 赔率反常规则 (再看赔率有没有"泄密")
  Layer 3: 赛中攻略规则 (滚球时的实时判读) — 禁用亚洲盘口
  Layer 4: 孙子兵法战术规则
  Layer 5: 规则参数优化器 (从历史数据学习最优阈值)

设计原则:
  1. 规则结构固定，数值参数化
  2. 所有数值阈值存放在 RULE_PARAMS 配置字典
  3. 可通过 train_rule_params.py 从历史数据优化参数
  4. 禁用亚洲盘口解读（用户规则）
================================================================
"""

from typing import Dict, List, Tuple, Optional
import numpy as np
import json
import os


# ====================================================================
# 规则参数配置（可通过训练优化）
# ====================================================================

DEFAULT_RULE_PARAMS = {
    # R2/R3: 赔率变化阈值
    'R2_odds_up_threshold': 1.05,    # 主胜赔率上升 > 5% 触发
    'R3_odds_down_threshold': 0.95,  # 主胜赔率下降 > 5% 触发
    
    # R4: 超低赔阈值
    'R4_ultra_low_threshold': 1.20,  # 主胜 < 1.20 触发
    
    # R6: 波胆检测（暂时无参数）
    
    # P1: 点球时间阈值（分钟）
    'P1_penalty_time': 10,
    
    # P2: 连进时间阈值（分钟）
    'P2_fast_goal_time': 16,
    
    # P3: 早进球时间阈值（分钟）
    'P3_early_goal_time': 25,
    
    # P4: 大小球阈值
    'P4_ou_threshold': 3.0,
    'P4_handicap_threshold': 0.25,
    
    # P5: 上半进球数阈值
    'P5_first_half_goals': 2,
    'P5_ou_range': [2.5, 3.0],
    
    # P6: 卡盘 O/U 值
    'P6_trap_ou': 3.0,
    
    # P7: 强队落后时间阈值
    'P7_comeback_time': 75,
    
    # P8: 连进检测时间窗口（分钟）
    'P8_fast_goal_window': 3,
    'P8_ou_threshold': 3.0,
    
    # 信号强度（可学习权重）
    'R1_signal_strength': 85,
    'R2_signal_strength': 65,
    'R3_signal_strength': 55,
    'R4_signal_strength': 40,
    'R6_signal_strength': 88,
    
    # 置信度（可学习）
    'P1_confidence': 0.85,
    'P2_confidence': 0.82,
    'P3_confidence': 0.80,
    'P4_confidence': 0.70,
    'P5_confidence': 0.75,
    'P6_confidence': 0.70,
    'P7_confidence': 0.72,
    'P8_confidence': 0.68,
}

# 参数配置路径
RULE_PARAMS_PATH = os.path.join(os.path.dirname(__file__), 'rule_params.json')


def load_rule_params() -> Dict:
    """加载规则参数（优先从文件，否则用默认）"""
    if os.path.exists(RULE_PARAMS_PATH):
        with open(RULE_PARAMS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return DEFAULT_RULE_PARAMS.copy()


def save_rule_params(params: Dict):
    """保存规则参数到文件"""
    os.makedirs(os.path.dirname(RULE_PARAMS_PATH), exist_ok=True)
    with open(RULE_PARAMS_PATH, 'w', encoding='utf-8') as f:
        json.dump(params, f, ensure_ascii=False, indent=2)


# ====================================================================
# LAYER 1: 比赛类型规则
# ====================================================================

MATCH_TYPE_RULES = {
    'world_cup_group': {
        'name': '世界杯分组赛',
        'goal_pattern': '无节制进攻',
        'over_trap_risk': 0.2,
        'draw_script_risk': 0.3,
        'blowout_likely': 0.7,
        'key_driver': '净胜球需求',
        'rules': [
            '强队<1.2 → 大概率赢3球以上 (7-1验证)',
            'O/U线飙升 → 不一定是诱Over，可能是真大球',
            '冷门方向反常 → 不需要过度解读',
        ]
    },
    'world_cup_knockout': {
        'name': '淘汰赛',
        'goal_pattern': '谨慎控制',
        'over_trap_risk': 0.4,
        'draw_script_risk': 0.5,
        'blowout_likely': 0.3,
        'key_driver': '晋级优先',
        'rules': [
            '平局赔率异常低 → 大概率90分钟平局',
            'O/U线低 → 大概率小球',
            '加时赛前不要买胜负',
        ]
    },
    'friendly': {
        'name': '友谊赛',
        'goal_pattern': '可控制',
        'over_trap_risk': 0.6,
        'draw_script_risk': 0.4,
        'blowout_likely': 0.3,
        'key_driver': '无利害关系',
        'rules': [
            'O/U线异常 → 高度警惕陷阱',
            '"连进后急停"框架适用',
            '冷门<5% → 更可能出冷门',
        ]
    },
    'league': {
        'name': '联赛',
        'goal_pattern': '视排名而定',
        'over_trap_risk': 0.35,
        'draw_script_risk': 0.3,
        'blowout_likely': 0.4,
        'key_driver': '积分需求',
    },
}


# ====================================================================
# LAYER 2: 赔率反常规则（参数化版本）
# ====================================================================

def get_odds_anomaly_rules(params: Dict = None) -> List[Dict]:
    """获取赔率反常规则，使用可学习参数"""
    if params is None:
        params = load_rule_params()
    
    return [
        {
            'id': 'R1',
            'name': '平局最低反常',
            'condition': lambda h, d, a: d <= h and d <= a,
            'stat': '856/312,010 (0.27%) → 平局命中率36.2%',
            'signal_strength': params.get('R1_signal_strength', 85),
            'action': '高度警惕平局！买平局或双选平局',
            'verified': True,
        },
        {
            'id': 'R2',
            'name': '主胜赔率上升',
            'condition': lambda h, oh, *args: h > oh * params.get('R2_odds_up_threshold', 1.05),
            'stat': '主胜率仅35.3% (vs正常44.3%)',
            'signal_strength': params.get('R2_signal_strength', 65),
            'action': '主胜赔升=危险信号，避免买主胜',
            'verified': True,
        },
        {
            'id': 'R3',
            'name': '主胜赔率下降',
            'condition': lambda h, oh, *args: h < oh * params.get('R3_odds_down_threshold', 0.95),
            'stat': '主胜率48.3% (vs正常44.3%)',
            'signal_strength': params.get('R3_signal_strength', 55),
            'action': '主胜赔降=正向信号，可跟随',
            'verified': True,
        },
        {
            'id': 'R4',
            'name': '超低赔冷门空间',
            'condition': lambda h, *args: h < params.get('R4_ultra_low_threshold', 1.20),
            'stat': '主胜85.8% → 仍有14.2%冷门空间',
            'signal_strength': params.get('R4_signal_strength', 40),
            'action': '超低赔≠稳赢！分组赛除外',
            'verified': True,
        },
        {
            'id': 'R6',
            'name': '最低波胆=答案',
            'condition': 'lowest_cs_direction != lowest_1x2_direction',
            'stat': '荷兰2-2 + 澳大利亚2-0 双验证',
            'signal_strength': params.get('R6_signal_strength', 88),
            'action': '全场最低波胆方向即为庄家真实预测 → 直接投注该方向',
            'verified': True,
        },
    ]


# ====================================================================
# LAYER 3: 赛中攻略规则（参数化版本，禁用亚洲盘口）
# ====================================================================

def get_inplay_rules(params: Dict = None) -> Dict:
    """获取赛中攻略规则，使用可学习参数"""
    if params is None:
        params = load_rule_params()
    
    return {
        'first_half': [
            {
                'id': 'P1',
                'name': '神盘-点球',
                'condition': lambda minute, *args: minute <= params.get('P1_penalty_time', 10),
                'signal': '上半场大概率还有球',
                'confidence': params.get('P1_confidence', 0.85),
                'source': '大神养成记-神盘',
            },
            {
                'id': 'P2',
                'name': '神盘-连进',
                'condition': lambda goals, minute, *args: goals >= 2 and minute <= params.get('P2_fast_goal_time', 16),
                'signal': '上半场大概率有第3/4球',
                'confidence': params.get('P2_confidence', 0.82),
                'source': '大神养成记-神盘',
            },
            # P3/P4 涉及亚洲盘口，仅作参考
            {
                'id': 'P5',
                'name': '穿盘信号',
                'condition': lambda fh_goals, ou: fh_goals >= params.get('P5_first_half_goals', 2) and ou >= params.get('P5_ou_range', [2.5, 3.0])[0],
                'signal': '下半场大概率继续进球',
                'confidence': params.get('P5_confidence', 0.75),
                'source': '大神养成记-穿盘判断',
            },
        ],
        'second_half': [
            {
                'id': 'P6',
                'name': '卡盘陷阱',
                'condition': lambda ou, *args: abs(ou - params.get('P6_trap_ou', 3.0)) < 0.1,
                'signal': '⚠️ 小球概率高，不要追大',
                'confidence': params.get('P6_confidence', 0.70),
                'source': '大神养成记-卡盘识别',
            },
            {
                'id': 'P8',
                'name': '连进后急停',
                'condition': lambda ou, fast_goals, time_window: ou > params.get('P8_ou_threshold', 3.0) and fast_goals >= 2 and time_window <= params.get('P8_fast_goal_window', 3),
                'signal': '⚠️ 接下来大概率冻结(Over陷阱)',
                'confidence': params.get('P8_confidence', 0.68),
                'source': '用户经验框架',
                'note': '⚠️ 世界杯分组赛不适用!',
            },
        ],
    }


# ====================================================================
# 综合决策入口
# ====================================================================

def classify_match(league: str) -> str:
    """判断比赛类型"""
    league_lower = league.lower()
    if 'world cup' in league_lower or '世界杯' in league:
        if 'group' in league_lower or '分组' in league:
            return 'world_cup_group'
        else:
            return 'world_cup_knockout'
    elif 'friendly' in league_lower or '友谊' in league:
        return 'friendly'
    else:
        return 'league'


def get_betting_decision(match_data: Dict, live_data: Dict = None, params: Dict = None) -> Dict:
    """
    综合决策入口
    
    决策逻辑:
      1. 判断比赛类型 → 选择规则框架
      2. 检测赔率反常信号 → 加权评分
      3. 如有滚球数据 → 应用攻略规则（禁用亚洲盘口）
      4. 输出最终建议
    """
    if params is None:
        params = load_rule_params()
    
    match_type = classify_match(match_data.get('league', ''))
    type_rules = MATCH_TYPE_RULES.get(match_type, MATCH_TYPE_RULES['league'])
    
    # 检测活跃的赔率规则
    active_rules = []
    close = match_data.get('close_odds', {})
    open_odds = match_data.get('open_odds', {})
    
    if close:
        h = close.get('H', 2)
        d = close.get('D', 3)
        a = close.get('A', 3)
        oh = open_odds.get('H', h) if open_odds else h
        
        # R1: 平局最低
        if d <= h and d <= a:
            active_rules.append('R1')
        
        # R2/R3: 赔率变化
        if open_odds:
            if h > oh * params.get('R2_odds_up_threshold', 1.05):
                active_rules.append('R2')
            elif h < oh * params.get('R3_odds_down_threshold', 0.95):
                active_rules.append('R3')
        
        # R4: 超低赔
        if h < params.get('R4_ultra_low_threshold', 1.20):
            active_rules.append('R4')
    
    # 综合判定
    verdict = {
        'match_type': match_type,
        'match_frame': type_rules,
        'active_rules': active_rules,
        'rule_params': params,
    }
    
    # 比赛类型特定的建议
    if match_type == 'world_cup_group':
        if h < 1.2:
            verdict['specific_advice'] = '分组赛强队屠杀模式，建议：买大球 + 让球胜'
        else:
            verdict['specific_advice'] = '按常规模型分析'
    elif match_type == 'world_cup_knockout':
        if d <= h and d <= a:
            verdict['specific_advice'] = '淘汰赛平局最低 → 大概率加时，90分钟买平'
    
    return verdict


# ====================================================================
# 规则参数优化器（训练接口）
# ====================================================================

def optimize_rule_params(historical_data: List[Dict], param_ranges: Dict = None, 
                         n_trials: int = 100) -> Dict:
    """
    从历史数据优化规则参数
    
    参数:
        historical_data: [{'open_odds': {...}, 'close_odds': {...}, 'result': 'H'/'D'/'A', ...}]
        param_ranges: 参数搜索范围，如 {'R2_odds_up_threshold': [1.03, 1.04, 1.05, 1.06, 1.07]}
        n_trials: 随机搜索试验次数
    
    返回:
        最优参数配置
    """
    import random
    
    if param_ranges is None:
        param_ranges = {
            'R2_odds_up_threshold': [1.03, 1.04, 1.05, 1.06, 1.07],
            'R3_odds_down_threshold': [0.93, 0.94, 0.95, 0.96, 0.97],
            'R4_ultra_low_threshold': [1.15, 1.18, 1.20, 1.22, 1.25],
            'R1_signal_strength': [75, 80, 85, 90, 95],
            'R6_signal_strength': [80, 85, 88, 90, 95],
        }
    
    best_params = DEFAULT_RULE_PARAMS.copy()
    best_score = 0.0
    
    for trial in range(n_trials):
        # 随机采样参数
        trial_params = DEFAULT_RULE_PARAMS.copy()
        for key, values in param_ranges.items():
            trial_params[key] = random.choice(values)
        
        # 评估参数
        score = _evaluate_params(trial_params, historical_data)
        
        if score > best_score:
            best_score = score
            best_params = trial_params.copy()
        
        if (trial + 1) % 10 == 0:
            print(f'  试验 {trial+1}/{n_trials}, 当前最优分数: {best_score:.4f}')
    
    print(f'\n✅ 最优参数分数: {best_score:.4f}')
    return best_params


def _evaluate_params(params: Dict, historical_data: List[Dict]) -> float:
    """评估一组参数的效果（准确率）"""
    correct = 0
    total = 0
    
    for match in historical_data:
        close = match.get('close_odds', {})
        open_odds = match.get('open_odds', {})
        result = match.get('result', '')
        
        if not close or not result:
            continue
        
        h = close.get('H', 2)
        d = close.get('D', 3)
        a = close.get('A', 3)
        oh = open_odds.get('H', h) if open_odds else h
        
        # 使用规则预测
        prediction = None
        
        # R1: 平局最低 → 预测 D
        if d <= h and d <= a:
            prediction = 'D'
        
        # R2: 主胜赔升 → 不预测 H
        elif h > oh * params.get('R2_odds_up_threshold', 1.05):
            prediction = 'D' if d < a else 'A'
        
        # R3: 主胜赔降 → 预测 H
        elif h < oh * params.get('R3_odds_down_threshold', 0.95):
            prediction = 'H'
        
        # 默认：使用最低赔率
        else:
            if h <= d and h <= a:
                prediction = 'H'
            elif d <= h and d <= a:
                prediction = 'D'
            else:
                prediction = 'A'
        
        if prediction == result:
            correct += 1
        total += 1
    
    return correct / total if total > 0 else 0.0


# ====================================================================
# 自检
# ====================================================================

if __name__ == '__main__':
    print('=' * 65)
    print('  footballAI 预测规则引擎 v1.0 — 参数化版本')
    print('=' * 65)
    
    params = load_rule_params()
    print(f'\n✅ 规则参数已加载: {len(params)} 个参数')
    print(f'  R2阈值: {params["R2_odds_up_threshold"]}')
    print(f'  R3阈值: {params["R3_odds_down_threshold"]}')
    print(f'  R4阈值: {params["R4_ultra_low_threshold"]}')
    
    # 测试决策
    test_match = {
        'league': 'World Cup Group Stage',
        'close_odds': {'H': 13.0, 'D': 6.70, 'A': 1.21},
        'open_odds': {'H': 12.0, 'D': 7.00, 'A': 1.18},
    }
    verdict = get_betting_decision(test_match, params=params)
    print(f'\n测试比赛: 卡塔尔 vs 瑞士')
    print(f'  比赛类型: {verdict["match_type"]}')
    print(f'  活跃规则: {verdict["active_rules"]}')
    print(f'  建议: {verdict.get("specific_advice", "按常规模型分析")}')
    
    print(f'\n✅ 预测规则引擎就绪')
