"""
================================================================
footballAI — 规则参数优化器 v1.0
================================================================

功能:
  1. 从历史数据库读取 1X2 + 结果
  2. 使用随机搜索 / 贝叶斯优化 调整规则参数
  3. 输出最优参数配置到 rules/rule_params.json

用法:
  python -m rules.train_rule_params --db data/football_data.db --n-trials 500
================================================================
"""

import argparse
import json
import os
import random
import sqlite3
from typing import Dict, List
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + '/..')

from rules.prediction_rules import (
    DEFAULT_RULE_PARAMS,
    optimize_rule_params,
    save_rule_params,
    load_rule_params,
)


def load_historical_data(db_path: str, min_date: str = '2020-01-01', 
                          max_date: str = '2024-12-31', 
                          limit: int = 50000) -> List[Dict]:
    """
    从数据库加载历史比赛数据
    
    字段需要:
      - open_home_odds, open_draw_odds, open_away_odds
      - close_home_odds, close_draw_odds, close_away_odds
      - final_result (H/D/A)
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    query = f'''
        SELECT 
            open_home_odds, open_draw_odds, open_away_odds,
            close_home_odds, close_draw_odds, close_away_odds,
            final_result,
            league,
            match_date
        FROM historical_matches 
        WHERE 
            close_home_odds IS NOT NULL 
            AND final_result IN ('H', 'D', 'A')
            AND match_date >= ?
            AND match_date <= ?
        ORDER BY RANDOM()
        LIMIT ?
    '''
    
    cursor.execute(query, (min_date, max_date, limit))
    rows = cursor.fetchall()
    conn.close()
    
    data = []
    for row in rows:
        open_h = row[0]
        open_d = row[1]
        open_a = row[2]
        close_h = row[3]
        close_d = row[4]
        close_a = row[5]
        result = row[6]
        league = row[7]
        match_date = row[8]
        
        # 过滤无效赔率
        if not (1.0 <= close_h <= 100.0 and 1.0 <= close_d <= 100.0 and 1.0 <= close_a <= 100.0):
            continue
        
        data.append({
            'open_odds': {'H': open_h, 'D': open_d, 'A': open_a} if open_h else None,
            'close_odds': {'H': close_h, 'D': close_d, 'A': close_a},
            'result': result,
            'league': league,
            'match_date': match_date,
        })
    
    print(f'✅ 加载历史数据: {len(data)} 场比赛')
    return data


def bayesian_optimize(historical_data: List[Dict], n_calls: int = 200) -> Dict:
    """
    贝叶斯优化（需要 scikit-optimize）
    
    如果 skopt 不可用，则回退到随机搜索
    """
    try:
        from skopt import gp_minimize
        from skopt.space import Real, Integer
        
        print('✅ 使用贝叶斯优化 (scikit-optimize)')
        
        # 定义搜索空间
        space = [
            Real(1.02, 1.10, name='R2_odds_up_threshold'),
            Real(0.90, 0.98, name='R3_odds_down_threshold'),
            Real(1.10, 1.30, name='R4_ultra_low_threshold'),
            Integer(70, 100, name='R1_signal_strength'),
            Integer(70, 100, name='R6_signal_strength'),
        ]
        
        def objective(params_list):
            params = DEFAULT_RULE_PARAMS.copy()
            keys = ['R2_odds_up_threshold', 'R3_odds_down_threshold', 
                     'R4_ultra_low_threshold', 'R1_signal_strength', 'R6_signal_strength']
            for k, v in zip(keys, params_list):
                params[k] = v
            
            # 负分数（因为 gp_minimize 是最小化）
            score = _evaluate_params(params, historical_data)
            return -score
        
        result = gp_minimize(
            objective,
            space,
            n_calls=n_calls,
            random_state=42,
            n_initial_points=20,
        )
        
        best_params = DEFAULT_RULE_PARAMS.copy()
        keys = ['R2_odds_up_threshold', 'R3_odds_down_threshold', 
                 'R4_ultra_low_threshold', 'R1_signal_strength', 'R6_signal_strength']
        for k, v in zip(keys, result.x):
            best_params[k] = v
        
        print(f'✅ 贝叶斯优化完成, 最优分数: {-result.fun:.4f}')
        return best_params
    
    except ImportError:
        print('⚠️ scikit-optimize 不可用, 回退到随机搜索')
        return random_search(historical_data, n_trials=500)


def random_search(historical_data: List[Dict], n_trials: int = 500) -> Dict:
    """随机搜索参数空间"""
    import itertools
    
    # 定义参数网格（简化的，避免组合爆炸）
    param_grid = {
        'R2_odds_up_threshold': [1.03, 1.04, 1.05, 1.06, 1.07, 1.08],
        'R3_odds_down_threshold': [0.92, 0.93, 0.94, 0.95, 0.96, 0.97],
        'R4_ultra_low_threshold': [1.15, 1.18, 1.20, 1.22, 1.25, 1.28],
        'R1_signal_strength': [75, 80, 85, 90, 95],
        'R6_signal_strength': [80, 85, 88, 90, 95],
    }
    
    # 如果组合数太大，随机采样
    all_combinations = list(itertools.product(*param_grid.values()))
    if len(all_combinations) > n_trials:
        sampled = random.sample(all_combinations, n_trials)
    else:
        sampled = all_combinations
    
    print(f'✅ 随机搜索: {len(sampled)} 组参数')
    
    best_params = DEFAULT_RULE_PARAMS.copy()
    best_score = 0.0
    
    keys = list(param_grid.keys())
    
    for i, values in enumerate(sampled):
        trial_params = DEFAULT_RULE_PARAMS.copy()
        for k, v in zip(keys, values):
            trial_params[k] = v
        
        score = _evaluate_params(trial_params, historical_data)
        
        if score > best_score:
            best_score = score
            best_params = trial_params.copy()
        
        if (i + 1) % 50 == 0:
            print(f'  进度: {i+1}/{len(sampled)}, 当前最优: {best_score:.4f}')
    
    print(f'✅ 随机搜索完成, 最优分数: {best_score:.4f}')
    return best_params


def _evaluate_params(params: Dict, historical_data: List[Dict]) -> float:
    """评估一组参数的效果（加权准确率）"""
    correct = 0
    total = 0
    weighted_correct = 0.0
    
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
        confidence = 0.33  # 默认置信度
        
        # R1: 平局最低 → 预测 D (高置信度)
        if d <= h and d <= a:
            prediction = 'D'
            confidence = params.get('R1_signal_strength', 85) / 100.0
        
        # R2: 主胜赔升 → 不预测 H
        elif open_odds and h > oh * params.get('R2_odds_up_threshold', 1.05):
            # 避免预测 H
            prediction = 'D' if d < a else 'A'
            confidence = params.get('R2_signal_strength', 65) / 100.0
        
        # R3: 主胜赔降 → 预测 H
        elif open_odds and h < oh * params.get('R3_odds_down_threshold', 0.95):
            prediction = 'H'
            confidence = params.get('R3_signal_strength', 55) / 100.0
        
        # R4: 超低赔 → 仍可能冷门
        elif h < params.get('R4_ultra_low_threshold', 1.20):
            prediction = 'H'  # 默认预测热门
            confidence = 0.80  # 高置信度但仍警惕冷门
        
        # 默认：使用最低赔率
        else:
            if h <= d and h <= a:
                prediction = 'H'
            elif d <= h and d <= a:
                prediction = 'D'
            else:
                prediction = 'A'
            confidence = 0.50  # 低置信度
        
        if prediction == result:
            correct += 1
            weighted_correct += confidence
        else:
            weighted_correct += (1 - confidence)  # 置信度低时错判惩罚小
        
        total += 1
    
    # 返回加权准确率
    return weighted_correct / total if total > 0 else 0.0


def main():
    parser = argparse.ArgumentParser(description='规则参数优化器')
    parser.add_argument('--db', type=str, default='data/football_data.db',
                        help='数据库路径')
    parser.add_argument('--n-trials', type=int, default=500,
                        help='随机搜索试验次数')
    parser.add_argument('--method', type=str, default='auto',
                        choices=['random', 'bayesian', 'auto'],
                        help='优化方法')
    parser.add_argument('--min-date', type=str, default='2020-01-01',
                        help='最小日期')
    parser.add_argument('--max-date', type=str, default='2024-12-31',
                        help='最大日期')
    parser.add_argument('--limit', type=int, default=50000,
                        help='最大加载比赛数')
    parser.add_argument('--output', type=str, default=None,
                        help='输出参数文件路径')
    
    args = parser.parse_args()
    
    print('=' * 65)
    print('  规则参数优化器 v1.0')
    print('=' * 65)
    
    # 加载历史数据
    print(f'\n>>> 加载历史数据: {args.db}')
    historical_data = load_historical_data(
        args.db, args.min_date, args.max_date, args.limit
    )
    
    if len(historical_data) < 1000:
        print('⚠️ 数据太少, 无法有效优化')
        return
    
    # 选择优化方法
    method = args.method
    if method == 'auto':
        try:
            import skopt
            method = 'bayesian'
        except ImportError:
            method = 'random'
    
    print(f'\n>>> 开始优化 (方法: {method})')
    
    if method == 'bayesian':
        best_params = bayesian_optimize(historical_data, n_calls=args.n_trials)
    else:
        best_params = random_search(historical_data, n_trials=args.n_trials)
    
    # 保存结果
    output_path = args.output or os.path.join(os.path.dirname(__file__), 'rule_params.json')
    save_rule_params(best_params)
    
    print(f'\n✅ 最优参数已保存到: {output_path}')
    print(f'\n最优参数:')
    for k, v in best_params.items():
        print(f'  {k}: {v}')
    
    # 验证优化效果
    print(f'\n>>> 验证优化效果...')
    original_score = _evaluate_params(DEFAULT_RULE_PARAMS, historical_data)
    optimized_score = _evaluate_params(best_params, historical_data)
    
    print(f'  原始参数分数: {original_score:.4f}')
    print(f'  优化参数分数: {optimized_score:.4f}')
    print(f'  提升: {optimized_score - original_score:+.4f}')


if __name__ == '__main__':
    main()
