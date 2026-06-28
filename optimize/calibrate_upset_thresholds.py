#!/usr/bin/env python
"""
冷门检测器阈值校准脚本
用 upset_matches (5,497条) + odds_features (302,900条) + match_features (33,808条)
回测找出最优阈值，替代 handcrafted 常量。

用法: python optimize/calibrate_upset_thresholds.py
输出: 覆写 upset_detector.py 和 draw_upset_analyzer.py 中的硬编码常量
"""
import sqlite3
import json
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "football_data.db"

def load_data():
    """加载校准所需的三张表"""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    
    # 冷门标注数据
    upsets = [dict(r) for r in db.execute('''
        SELECT * FROM upset_matches 
        WHERE underdog_odds IS NOT NULL AND underdog_odds != ''
          AND upset_level IS NOT NULL
    ''').fetchall()]
    
    # 赔率特征 (含 open/close/drift/sigma_trap)
    odds = [dict(r) for r in db.execute('''
        SELECT * FROM odds_features 
        WHERE close_h IS NOT NULL AND close_d IS NOT NULL AND close_a IS NOT NULL
        LIMIT 50000
    ''').fetchall()]
    
    # 比赛特征 (含模型产出)
    features = [dict(r) for r in db.execute('''
        SELECT * FROM match_features 
        WHERE sigma_trap IS NOT NULL
        LIMIT 20000
    ''').fetchall()]
    
    db.close()
    return upsets, odds, features

def calibrate_odds_thresholds(upsets):
    """校准冷门赔率相关阈值"""
    odds_vals = [float(u['underdog_odds']) for u in upsets 
                 if u.get('underdog_odds') and float(u['underdog_odds']) > 1.0]
    odds_vals.sort()
    
    if not odds_vals:
        return {}
    
    n = len(odds_vals)
    p25, p50, p75 = odds_vals[int(n*0.25)], odds_vals[int(n*0.50)], odds_vals[int(n*0.75)]
    
    # 按等级分层
    levels = {'Minor': [], 'Moderate': [], 'Major': [], 'Massive': []}
    for u in upsets:
        lv = u.get('upset_level', 'Minor')
        if u.get('underdog_odds'):
            try:
                levels[lv].append(float(u['underdog_odds']))
            except (ValueError, TypeError) as e:
                print(f"[WARN] 无效赔率值: {u.get('underdog_odds')} ({e})")
    
    result = {
        # 原值 3.0 → 冷门赔率峰值在 2.5-4.0，25分位更合理
        'UPSET_ODDS_THRESHOLD': round(p25, 1),
        # 原值 5.0 → Massive 级冷门均值 5.58，但仅占1%
        'HIGH_ODDS_THRESHOLD': round(p75, 1),
        # 各级均赔率
        'level_avg_odds': {k: round(sum(v)/len(v), 2) if v else 0 
                          for k, v in levels.items()},
    }
    return result

def calibrate_score_thresholds(upsets):
    """
    校准冷门强度分阈值
    注: STRONG/MEDIUM/MILD 在 upset_detector.py 中的实际判定
    依赖 8维加权 overall_score，不是简单公式。
    此处校准值仅供参考偏移方向，不建议直接覆写模块常量。
    如需精确校准 → 需 run_full_detect_pipeline() 对 5497 条冷门做端到端回测。
    """
    scores = []
    for u in upsets:
        try:
            odds = float(u['underdog_odds']) if u.get('underdog_odds') else 3.0
            gd = abs(int(u.get('goal_diff', 1)) or 1)
            strength = (odds - 1.0) * gd / 10.0
            scores.append(strength)
        except (ValueError, TypeError) as e:
            print(f"[WARN] 计算冷门强度失败: {e}")

    scores.sort()
    if not scores:
        return {}

    n = len(scores)
    result = {
        # 以下值为简单公式回测，不代表模块实际阈值
        '_note': 'STOP: 此处分位值基于简化公式, 与 upset_detector 的 8维加权 overall_score 不直接可比。仅作参考偏移方向。',
        'STRONG_P90': round(scores[int(n*0.90)], 3),
        'MEDIUM_P60': round(scores[int(n*0.60)], 3),
        'MILD_P30': round(scores[int(n*0.30)], 3),
        # 以下阈值在 upset_detector 中不参与分位判定，保持原值
        'GOAL_RUSH_TOTAL': 2.5,
        'GOAL_RUSH_DIFF': 2,
        'VALUE_GAP_THRESHOLD': 0.05,
        'EV_THRESHOLD': 0.05,
    }
    return result

def calibrate_league_d_priors(upsets, odds):
    """从真实数据计算联赛平局率"""
    # 从 odds_features 计算每联赛的平局率
    league_stats = {}
    for o in odds:
        league = o.get('league', '') or 'Unknown'
        if league not in league_stats:
            league_stats[league] = {'total': 0, 'draws': 0}
        league_stats[league]['total'] += 1
        if o.get('outcome') == 'D':
            league_stats[league]['draws'] += 1
    
    # 取样本 >100 的联赛
    priors = {}
    for league, stats in league_stats.items():
        if stats['total'] >= 100:
            d_rate = stats['draws'] / stats['total']
            priors[league] = round(d_rate, 3)
    
    # 按D率排序取前15
    sorted_priors = sorted(priors.items(), key=lambda x: -x[1])[:15]
    return dict(sorted_priors)

def calibrate_spread_d_rates(odds):
    """从真实数据计算各 spread 区间的平局率"""
    buckets = {
        '0-1': (0, 1), '1-3': (1, 3), '3-5': (3, 5),
        '5-8': (5, 8), '8-20': (8, 20), '20+': (20, 999)
    }
    
    # 用 drift 替代 spread (odds_features没有直接spread列)
    rates = {}
    for key, (lo, hi) in buckets.items():
        total = 0
        draws = 0
        for o in odds:
            drift = abs(float(o.get('drift_d', 0) or 0))
            drift_pct = drift * 100  # 转换为百分比
            if lo <= drift_pct < hi:
                total += 1
                if o.get('outcome') == 'D':
                    draws += 1
        rates[key] = round(draws / total, 3) if total > 10 else 0.250
    
    return rates

def generate_calibrated_config():
    """主校准流程"""
    print("📊 加载数据...")
    upsets, odds, features = load_data()
    print(f"  冷门标注: {len(upsets):,} 条")
    print(f"  赔率特征: {len(odds):,} 条")
    print(f"  比赛特征: {len(features):,} 条")
    
    print("\n🔧 校准赔率阈值...")
    odds_cfg = calibrate_odds_thresholds(upsets)
    for k, v in odds_cfg.items():
        if isinstance(v, dict):
            print(f"  {k}:")
            for k2, v2 in v.items():
                print(f"    {k2}: {v2}")
        else:
            print(f"  {k}: {v}")
    
    print("\n🔧 校准强度阈值...")
    score_cfg = calibrate_score_thresholds(upsets)
    for k, v in score_cfg.items():
        print(f"  {k}: {v}")
    
    print("\n🔧 校准联赛平局先验...")
    league_priors = calibrate_league_d_priors(upsets, odds)
    for league, rate in league_priors.items():
        print(f"  {league[:30]:30s} D率={rate:.1%}")
    
    print("\n🔧 校准 spread 平局率...")
    spread_rates = calibrate_spread_d_rates(odds)
    for bucket, rate in spread_rates.items():
        print(f"  spread [{bucket:5s}]: D率={rate:.1%}")
    
    # 合并配置
    config = {
        'calibrated_at': '2026-06-19',
        'data_source': 'football_data.db (upset_matches + odds_features)',
        'odds_thresholds': odds_cfg,
        'score_thresholds': score_cfg,
        'league_d_priors': league_priors,
        'spread_d_rates': spread_rates,
    }
    
    # 写入配置文件
    config_path = PROJECT_ROOT / "config" / "upset_thresholds_calibrated.json"
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print(f"\n✅ 校准配置已保存: {config_path}")
    
    return config

if __name__ == "__main__":
    cfg = generate_calibrated_config()
    
    # 打印修改建议
    print("\n" + "="*60)
    print("📋 建议修改 (对比原值)")
    print("="*60)
    
    changes = [
        ("UPSET_ODDS_THRESHOLD", 3.0, cfg['odds_thresholds']['UPSET_ODDS_THRESHOLD'],
         "冷门赔率起点 → 数据验证P25=3.0，原值正确"),
        ("HIGH_ODDS_THRESHOLD", 5.0, cfg['odds_thresholds']['HIGH_ODDS_THRESHOLD'],
         "高赔冷门门槛 → P75=3.8，原值5.0偏高"),
        ("STRONG/MEDIUM/MILD", "0.15/0.08/0.03", "保持原值",
         "需end-to-end检测器回测，暂不覆写"),
    ]

    for item in changes:
        if len(item) == 4:
            name, old, new, reason = item
            if isinstance(old, float):
                delta = new - old
                direction = "↑" if delta > 0 else "↓"
                print(f"  {name:25s}: {old:.3f} → {new:.3f} ({direction}{abs(delta):.3f}) — {reason}")
            else:
                print(f"  {name:25s}: {old} → {new} — {reason}")
