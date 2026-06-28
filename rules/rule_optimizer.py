"""
================================================================
footballAI — 规则参数优化器 v1.0
================================================================

功能:
  1. 从历史数据库读取比赛 + 赔率 + 结果
  2. 使用随机搜索 / 贝叶斯优化 调整规则参数
  3. 输出最优参数配置到 rules/rule_params.json

用法:
  python rules/rule_optimizer.py --db data/football_data.db --method random --n-trials 200
  python rules/rule_optimizer.py --db data/football_data.db --method bayesian --n-trials 100
================================================================
"""

import argparse
import json
import os
import random
import sqlite3
from typing import Dict, List
import sys

# 添加项目根目录到路径

from rules.prediction_rules import (
    DEFAULT_RULE_PARAMS,
    load_rule_params,
    save_rule_params,
    _evaluate_params,
)

def load_historical_data(
    db_path: str,
    min_date: str = '2020-01-01',
    max_date: str = '2024-12-31',
    limit: int = 50000,
    league_filter: str = None,
) -> List[Dict]:
    """
    从数据库加载历史比赛数据（用于规则参数优化）
    
    必需字段:
      - open_home_odds, open_draw_odds, open_away_odds
      - close_home_odds, close_draw_odds, close_away_odds
      - final_result (H/D/A)
    """
    if not os.path.exists(db_path):
        print(f'⚠️ 数据库不存在: {db_path}')
        return []
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 检查表结构
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='historical_matches'")
    if not cursor.fetchone():
        print('⚠️ 表 historical_matches 不存在')
        conn.close()
        return []
    
    # 构建查询
    query = '''
        SELECT 
            open_home_odds, open_draw_odds, open_away_odds,
            close_home_odds, close_draw_odds, close_away_odds,
            final_result,
            league_name,
            match_date
        FROM historical_matches 
        WHERE 
            close_home_odds IS NOT NULL 
            AND final_result IN ('H', 'D', 'A')
    '''
    params = []
    
    if min_date:
        query += ' AND match_date >= ?'
        params.append(min_date)
    
    if max_date:
        query += ' AND match_date <= ?'
        params.append(max_date)
    
    if league_filter:
        query += ' AND league_name LIKE ?'
        params.append(f'%{league_filter}%')
    
    query += ' ORDER BY RANDOM() LIMIT ?'
    params.append(limit)
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    data = []
    skipped = 0
    
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
            skipped += 1
            continue
        
        data.append({
            'open_odds': {'H': open_h, 'D': open_d, 'A': open_a} if open_h else None,
            'close_odds': {'H': close_h, 'D': close_d, 'A': close_a},
            'result': result,
            'league_name': league,  # 数据库字段名
            'match_date': match_date,
        })
    
    print(f'✅ 加载历史数据: {len(data)} 场比赛 (跳过 {skipped} 条无效数据)')
    return data

def random_search(
    historical_data: List[Dict],
    n_trials: int = 500,
    param_ranges: Dict = None,
) -> Dict:
    """
    随机搜索参数空间
    
    返回:
        最优参数配置
    """
    if param_ranges is None:
        param_ranges = {
            'R2_odds_up_threshold': [1.03, 1.04, 1.05, 1.06, 1.07, 1.08],
            'R3_odds_down_threshold': [0.92, 0.93, 0.94, 0.95, 0.96, 0.97],
            'R4_ultra_low_threshold': [1.15, 1.18, 1.20, 1.22, 1.25, 1.28],
            'R1_signal_strength': [75, 80, 85, 90, 95],
            'R6_signal_strength': [80, 85, 88, 90, 95],
        }
    
    import itertools
    
    # 如果组合数太大，随机采样
    keys = list(param_ranges.keys())
    all_combinations = list(itertools.product(*[param_ranges[k] for k in keys]))
    
    if len(all_combinations) > n_trials:
        sampled = random.sample(all_combinations, n_trials)
    else:
        sampled = all_combinations
    
    print(f'✅ 随机搜索: {len(sampled)} 组参数')
    
    best_params = DEFAULT_RULE_PARAMS.copy()
    best_score = 0.0
    
    for i, values in enumerate(sampled):
        trial_params = DEFAULT_RULE_PARAMS.copy()
        for k, v in zip(keys, values):
            trial_params[k] = v
        
        score = _evaluate_params(trial_params, historical_data)
        
        if score > best_score:
            best_score = score
            best_params = trial_params.copy()
            print(f'  [{i+1}/{len(sampled)}] 新最优: {best_score:.4f} | {best_params}')
        
        elif (i + 1) % 50 == 0:
            print(f'  进度: {i+1}/{len(sampled)}, 当前最优: {best_score:.4f}')
    
    print(f'\n✅ 随机搜索完成, 最优分数: {best_score:.4f}')
    return best_params

def bayesian_optimize(
    historical_data: List[Dict],
    n_calls: int = 200,
) -> Dict:
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
            acq_func='EI',
        )
        
        best_params = DEFAULT_RULE_PARAMS.copy()
        keys = ['R2_odds_up_threshold', 'R3_odds_down_threshold', 
                 'R4_ultra_low_threshold', 'R1_signal_strength', 'R6_signal_strength']
        for k, v in zip(keys, result.x):
            best_params[k] = float(v) if isinstance(v, (int, float)) else v
        
        print(f'✅ 贝叶斯优化完成, 最优分数: {-result.fun:.4f}')
        return best_params
    
    except ImportError:
        print('⚠️ scikit-optimize 不可用, 回退到随机搜索')
        return random_search(historical_data, n_trials=500)

def evaluate_current_params(historical_data: List[Dict]) -> Dict:
    """评估当前参数配置的效果"""
    current_params = load_rule_params()
    score = _evaluate_params(current_params, historical_data)
    
    print(f'\n当前参数评估:')
    print(f'  加权准确率: {score:.4f}')
    print(f'  参数:')
    for k, v in current_params.items():
        print(f'    {k}: {v}')
    
    return {'score': score, 'params': current_params}

def main():
    parser = argparse.ArgumentParser(description='footballAI 规则参数优化器')
    parser.add_argument('--db', type=str, default='data/football_data.db',
                        help='数据库路径 (默认: data/football_data.db)')
    parser.add_argument('--method', type=str, default='auto',
                        choices=['random', 'bayesian', 'auto'],
                        help='优化方法 (默认: auto)')
    parser.add_argument('--n-trials', type=int, default=200,
                        help='随机搜索试验次数 / 贝叶斯优化调用次数 (默认: 200)')
    parser.add_argument('--min-date', type=str, default='2020-01-01',
                        help='最小比赛日期 (默认: 2020-01-01)')
    parser.add_argument('--max-date', type=str, default='2024-12-31',
                        help='最大比赛日期 (默认: 2024-12-31)')
    parser.add_argument('--limit', type=int, default=50000,
                        help='最大加载比赛数 (默认: 50000)')
    parser.add_argument('--league', type=str, default=None,
                        help='联赛过滤 (如: Premier League)')
    parser.add_argument('--output', type=str, default=None,
                        help='输出参数文件路径 (默认: rules/rule_params.json)')
    parser.add_argument('--evaluate-only', action='store_true',
                        help='仅评估当前参数，不优化')
    
    args = parser.parse_args()
    
    print('=' * 65)
    print('  footballAI — 规则参数优化器 v1.0')
    print('=' * 65)
    
    # 加载历史数据
    print(f'\n>>> 加载历史数据: {args.db}')
    historical_data = load_historical_data(
        args.db,
        min_date=args.min_date,
        max_date=args.max_date,
        limit=args.limit,
        league_filter=args.league,
    )
    
    if len(historical_data) < 1000:
        print('⚠️ 数据太少 (< 1000 场), 无法有效优化')
        return
    
    # 仅评估模式
    if args.evaluate_only:
        evaluate_current_params(historical_data)
        return
    
    # 选择优化方法
    method = args.method
    if method == 'auto':
        try:
            import skopt  # noqa
            method = 'bayesian'
        except ImportError:
            method = 'random'
    
    print(f'\n>>> 开始优化 (方法: {method}, 试验次数: {args.n_trials})')
    
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
    current_score = _evaluate_params(load_rule_params(), historical_data)
    
    print(f'  默认参数分数: {original_score:.4f}')
    print(f'  优化参数分数: {optimized_score:.4f}')
    print(f'  当前参数分数: {current_score:.4f}')
    print(f'  提升 (vs 默认): {optimized_score - original_score:+.4f}')
    print(f'  提升 (vs 当前): {optimized_score - current_score:+.4f}')
    
    print(f'\n✅ 优化完成')

if __name__ == '__main__':
    main()
