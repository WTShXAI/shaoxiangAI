#!/usr/bin/env python3
"""
赔率尾数理论验证脚本 v1.0
验证涛哥4大操盘尾数理论 + 独赢/大小球交叉验证 + 平手盘信号
数据源: historical_matches (312K) + feature_library (2.5K OU)
"""

import sqlite3
import json
import sys
from datetime import datetime
from collections import defaultdict, Counter

def connect_hist():
    return sqlite3.connect('data/football_data.db')

def connect_fl():
    return sqlite3.connect('data/shaoxiang_feature_library.db')

def round_to_dec(odds, n=2):
    """Extract last n decimal digits as integer"""
    if odds is None or odds <= 0:
        return None
    return int(round(odds, 2) * 100) % (10**n)

def odds_tail(odds):
    """Get the last 2 decimal digits as integer (e.g. 1.44 -> 44, 3.18 -> 18)"""
    if odds is None or odds <= 0:
        return None
    return int(round(odds, 2) * 100) % 100

def odds_in_range(odds, low, high):
    """Check if odds is in [low, high)"""
    return odds is not None and low <= odds < high

# ============================================================
# THEORY 1: 1.44 死亡尾数
# Claim: 主胜1.44时爆冷(非主胜)概率>36%
# ============================================================
def validate_death_tail_144(c):
    """Validate 1.44 death tail theory"""
    results = c.execute('''
        SELECT close_home_odds, final_result
        FROM historical_matches
        WHERE close_home_odds > 0 AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # Bin by odds rounded to 2dp
    bins = defaultdict(lambda: {'H':0, 'D':0, 'A':0, 'total':0})
    
    for odds, res in results:
        tail = round(odds * 100)  # integer cents
        if tail >= 100 and tail <= 600:  # 1.00-6.00 range
            bins[tail][res] += 1
            bins[tail]['total'] += 1
    
    # Focus on 144 (1.44)
    target_144 = bins.get(144, None)
    
    # Compare: all 1.40-1.49 (nearby "safe" range)
    nearby = {'H':0, 'D':0, 'A':0, 'total':0}
    for tail, cnt in bins.items():
        odds_val = tail / 100
        if 1.40 <= odds_val < 1.50:
            nearby['H'] += cnt['H']
            nearby['D'] += cnt['D']
            nearby['A'] += cnt['A']
            nearby['total'] += cnt['total']
    
    # Baseline: all matches with odds 1.30-1.60
    baseline = {'H':0, 'D':0, 'A':0, 'total':0}
    for tail, cnt in bins.items():
        odds_val = tail / 100
        if 1.30 <= odds_val < 1.60:
            baseline['H'] += cnt['H']
            baseline['D'] += cnt['D']
            baseline['A'] += cnt['A']
            baseline['total'] += cnt['total']
    
    upset_144 = (target_144['D'] + target_144['A']) / target_144['total'] * 100 if target_144 and target_144['total'] > 0 else 0
    upset_nearby = (nearby['D'] + nearby['A']) / nearby['total'] * 100 if nearby['total'] > 0 else 0
    upset_baseline = (baseline['D'] + baseline['A']) / baseline['total'] * 100 if baseline['total'] > 0 else 0
    
    return {
        'theory': '1.44死亡尾数',
        'claim': '爆冷概率>36%',
        'data': {
            '1.44_samples': target_144['total'] if target_144 else 0,
            '1.44_upset_rate': round(upset_144, 2),
            '1.40_1.49_upset_rate': round(upset_nearby, 2),
            '1.30_1.60_upset_rate': round(upset_baseline, 2),
        },
        'verdict': 'PASS' if upset_144 > 36 else f'BELOW ({upset_144:.1f}% < 36%)',
        'detail_144': {
            'H': target_144['H'] if target_144 else 0,
            'D': target_144['D'] if target_144 else 0,
            'A': target_144['A'] if target_144 else 0,
            'total': target_144['total'] if target_144 else 0,
        }
    }

# ============================================================
# THEORY 2: 1.99/2.99 临界尾数
# Claim: 1.99打出率<2.00, 散户下注意愿高30%, 诱盘概率高28%
# ============================================================
def validate_critical_tail_199(c):
    """Validate 1.99/2.99 critical tail theory"""
    results = c.execute('''
        SELECT close_home_odds, final_result
        FROM historical_matches
        WHERE close_home_odds > 0 AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # Group by exact 2dp odds
    bins = defaultdict(lambda: {'H':0, 'D':0, 'A':0, 'total':0})
    for odds, res in results:
        cents = round(odds * 100)
        if 100 <= cents <= 500:
            bins[cents][res] += 1
            bins[cents]['total'] += 1
    
    def get_win_rate(cents_val):
        d = bins.get(cents_val, {'H':0,'total':1})
        return d['H'] / d['total'] * 100 if d['total'] > 0 else 0
    
    def get_sample_count(cents_val):
        return bins.get(cents_val, {'total':0})['total']
    
    # Compare: 199 vs 200, 299 vs 300
    results_199 = {
        '199': {'rate': get_win_rate(199), 'n': get_sample_count(199)},
        '200': {'rate': get_win_rate(200), 'n': get_sample_count(200)},
        '299': {'rate': get_win_rate(299), 'n': get_sample_count(299)},
        '300': {'rate': get_win_rate(300), 'n': get_sample_count(300)},
    }
    
    # Also check 149/150, 249/250, 349/350 for pattern
    extended = {}
    for cen in [149, 150, 249, 250, 349, 350]:
        extended[str(cen)] = {'rate': get_win_rate(cen), 'n': get_sample_count(cen)}
    
    diff_199_200 = results_199['199']['rate'] - results_199['200']['rate']
    diff_299_300 = results_199['299']['rate'] - results_199['300']['rate']
    
    return {
        'theory': '1.99/2.99临界尾数',
        'claim': '1.99打出率<2.00 (诱盘)',
        'data': {
            '1.99_win_rate': round(results_199['199']['rate'], 2),
            '2.00_win_rate': round(results_199['200']['rate'], 2),
            'diff_199_vs_200': round(diff_199_200, 2),
            '2.99_win_rate': round(results_199['299']['rate'], 2),
            '3.00_win_rate': round(results_199['300']['rate'], 2),
            'diff_299_vs_300': round(diff_299_300, 2),
            '1.99_samples': results_199['199']['n'],
            '2.00_samples': results_199['200']['n'],
            '2.99_samples': results_199['299']['n'],
            '3.00_samples': results_199['300']['n'],
        },
        'extended_pattern': {k: {'rate': round(v['rate'],2), 'n': v['n']} for k,v in extended.items()},
        'verdict': 'PASS' if (diff_199_200 < 0 or diff_299_300 < 0) else 'NO_EFFECT',
        'verdict_detail': f'1.99 vs 2.00: {diff_199_200:+.1f}pp | 2.99 vs 3.00: {diff_299_300:+.1f}pp'
    }

# ============================================================
# THEORY 3: 平赔尾数带8
# Claim: 平赔尾数带8 (e.g. 3.18, 2.88) → 平局概率比同档位高30%
# ============================================================
def validate_draw_tail_8(c):
    """Validate draw odds tail 8 theory"""
    results = c.execute('''
        SELECT close_draw_odds, final_result
        FROM historical_matches
        WHERE close_draw_odds > 0 AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # Bin by draw odds (0.05 granularity)
    tail8 = {'H':0, 'D':0, 'A':0, 'total':0}
    non_tail8 = {'other': {'H':0, 'D':0, 'A':0, 'total':0}}
    
    for odds, res in results:
        tail = odds_tail(odds)
        if tail is None: continue
        # Only consider draw odds 2.00-5.00 (main range)
        if 2.00 <= odds < 5.00:
            if tail % 10 == 8:  # ends with 8
                tail8[res] += 1
                tail8['total'] += 1
            else:
                non_tail8['other'][res] += 1
                non_tail8['other']['total'] += 1
    
    # Also compare per odds tier
    tier_data = {}
    for tier_low in [2.0, 2.5, 3.0, 3.5, 4.0]:
        tier_high = tier_low + 0.5
        tail8_tier = {'H':0, 'D':0, 'A':0, 'total':0}
        other_tier = {'H':0, 'D':0, 'A':0, 'total':0}
        
        for odds, res in results:
            if tier_low <= odds < tier_high:
                tail = odds_tail(odds)
                if tail is not None:
                    if tail % 10 == 8:
                        tail8_tier[res] += 1
                        tail8_tier['total'] += 1
                    else:
                        other_tier[res] += 1
                        other_tier['total'] += 1
        
        t8_rate = tail8_tier['D']/tail8_tier['total']*100 if tail8_tier['total'] > 10 else 0
        other_rate = other_tier['D']/other_tier['total']*100 if other_tier['total'] > 10 else 0
        tier_data[f'{tier_low}-{tier_high}'] = {
            'tail8_n': tail8_tier['total'], 'tail8_draw_rate': round(t8_rate, 2),
            'other_n': other_tier['total'], 'other_draw_rate': round(other_rate, 2),
            'diff_pp': round(t8_rate - other_rate, 2)
        }
    
    overall_t8_rate = tail8['D']/tail8['total']*100 if tail8['total'] > 0 else 0
    overall_other_rate = non_tail8['other']['D']/non_tail8['other']['total']*100 if non_tail8['other']['total'] > 0 else 0
    
    return {
        'theory': '平赔尾数带8',
        'claim': '平局概率比同档位高30%(相对)',
        'data': {
            'tail8_total': tail8['total'],
            'tail8_draw_rate': round(overall_t8_rate, 2),
            'other_total': non_tail8['other']['total'],
            'other_draw_rate': round(overall_other_rate, 2),
            'relative_diff': round((overall_t8_rate - overall_other_rate) / overall_other_rate * 100, 1) if overall_other_rate > 0 else 0,
            'absolute_diff_pp': round(overall_t8_rate - overall_other_rate, 2),
        },
        'by_tier': tier_data,
        'verdict': 'PASS' if overall_t8_rate > overall_other_rate else 'NO_EFFECT'
    }

# ============================================================
# THEORY 4: 高赔率尾数带4
# Claim: 高赔率(>4.0)尾数带4打出概率比同档位高20%(相对)
# ============================================================
def validate_high_odds_tail_4(c):
    """Validate high odds tail 4 theory"""
    results = c.execute('''
        SELECT 
            CASE 
                WHEN final_result='H' AND close_home_odds >= close_away_odds THEN close_home_odds
                WHEN final_result='A' AND close_away_odds >= close_home_odds THEN close_away_odds
                ELSE NULL
            END as underdog_odds,
            CASE 
                WHEN final_result='H' AND close_home_odds >= close_away_odds THEN 1
                WHEN final_result='A' AND close_away_odds >= close_home_odds THEN 1
                ELSE 0
            END as underdog_won
        FROM historical_matches
        WHERE close_home_odds > 0 AND close_away_odds > 0 
        AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # Bin by odds
    tail4 = {'won': 0, 'total': 0}
    other_high = {'won': 0, 'total': 0}
    
    for odds, won in results:
        if odds is None: continue
        if odds >= 4.0:  # high odds
            tail = odds_tail(odds)
            if tail is not None:
                if tail % 10 == 4:
                    tail4['won'] += won
                    tail4['total'] += 1
                else:
                    other_high['won'] += won
                    other_high['total'] += 1
    
    tail4_rate = tail4['won']/tail4['total']*100 if tail4['total'] > 0 else 0
    other_rate = other_high['won']/other_high['total']*100 if other_high['total'] > 0 else 0
    
    # Per tier
    tier_data = {}
    for tier_low in [4.0, 5.0, 6.0]:
        tier_high = tier_low + 1.0
        t4_tier = {'won': 0, 'total': 0}
        o_tier = {'won': 0, 'total': 0}
        for odds, won in results:
            if odds and tier_low <= odds < tier_high:
                tail = odds_tail(odds)
                if tail is not None:
                    if tail % 10 == 4:
                        t4_tier['won'] += won
                        t4_tier['total'] += 1
                    else:
                        o_tier['won'] += won
                        o_tier['total'] += 1
        tier_data[f'{tier_low}-{tier_high}'] = {
            'tail4_n': t4_tier['total'], 'tail4_rate': round(t4_tier['won']/t4_tier['total']*100,2) if t4_tier['total']>5 else 0,
            'other_n': o_tier['total'], 'other_rate': round(o_tier['won']/o_tier['total']*100,2) if o_tier['total']>5 else 0,
        }
    
    return {
        'theory': '高赔率尾数带4',
        'claim': '打出概率比同档位高20%(相对)',
        'data': {
            'tail4_total': tail4['total'],
            'tail4_win_rate': round(tail4_rate, 2),
            'other_total': other_high['total'],
            'other_win_rate': round(other_rate, 2),
            'relative_diff': round((tail4_rate - other_rate) / other_rate * 100, 1) if other_rate > 0 else 0,
            'absolute_diff_pp': round(tail4_rate - other_rate, 2),
        },
        'by_tier': tier_data,
        'verdict': 'PASS' if tail4_rate > other_rate else 'NO_EFFECT'
    }

# ============================================================
# THEORY 5: 1X2+OU交叉验证
# Claim 5a: 强队低胜赔(≤1.5)+2.5球高水(≥0.95) → 强队小胜
# Claim 5b: 平赔走低+2.75降赔 → 平局+大球
# ============================================================
def validate_1x2_ou_cross(c_hist, c_fl):
    """Validate 1X2 + OU cross-validation formulas"""
    
    # Load OU data from feature library
    ou_data = c_fl.execute('''
        SELECT 
            f.match_id, f.home, f.away, f.league,
            f.xou_line, f.xou_over, f.xou_under, f.label_ou,
            f.odds_h, f.odds_d, f.odds_a,
            m.home_score, m.away_score
        FROM features f
        LEFT JOIN matches m ON f.match_id = m.match_id
        WHERE f.xou_line > 0 AND f.label_ou IS NOT NULL
    ''').fetchall()
    
    if len(ou_data) < 100:
        return {
            'theory': '1X2+OU交叉验证',
            'warning': f'仅{len(ou_data)}条OU数据，样本不足',
            'data': {}
        }
    
    # 5a: 强队低赔 + 2.5球高水 → 小胜 (总进球≤2)
    strong_fav_low_goals = {'matches': 0, 'low_goals': 0, 'high_goals': 0}
    for row in ou_data:
        mid, home, away, league, xou_line, xou_over, xou_under, label_ou, odds_h, odds_d, odds_a, hs, aws = row
        if odds_h is None or xou_over is None: continue
        if odds_h <= 1.5 and abs(xou_line - 2.5) < 0.05 and xou_over > 0.95:
            total = (hs or 0) + (aws or 0)
            strong_fav_low_goals['matches'] += 1
            if total <= 2:
                strong_fav_low_goals['low_goals'] += 1
            else:
                strong_fav_low_goals['high_goals'] += 1
    
    # 5b: 平赔走低(3.0-3.5) + 2.75球降赔 → 平局+大球
    draw_low_275 = {'matches': 0, 'draws': 0, 'high_goals': 0}
    for row in ou_data:
        mid, home, away, league, xou_line, xou_over, xou_under, label_ou, odds_h, odds_d, odds_a, hs, aws = row
        if odds_d is None or xou_under is None: continue
        if 3.0 <= odds_d <= 3.5 and abs(xou_line - 2.75) < 0.05 and xou_under < 0.85:
            # xou_under < 0.85 means bookmaker lowered draw+under → expecting goals
            draw_low_275['matches'] += 1
            total = (hs or 0) + (aws or 0)
            if hs == aws:  # draw
                draw_low_275['draws'] += 1
            if total >= 3:
                draw_low_275['high_goals'] += 1
    
    return {
        'theory': '1X2+OU交叉验证',
        'formula_5a': {
            '描述': '强队胜赔≤1.5 + 2.5球高水(over>0.95) → 小胜',
            'samples': strong_fav_low_goals['matches'],
            '低进球率(≤2)': round(strong_fav_low_goals['low_goals']/max(strong_fav_low_goals['matches'],1)*100,1),
        } if strong_fav_low_goals['matches'] >= 5 else {'描述': '样本不足(<5)'},
        'formula_5b': {
            '描述': '平赔3.0-3.5 + 2.75球降赔(under<0.85) → 平局+大球',
            'samples': draw_low_275['matches'],
            '平局率': round(draw_low_275['draws']/max(draw_low_275['matches'],1)*100,1),
            '大球率(≥3)': round(draw_low_275['high_goals']/max(draw_low_275['matches'],1)*100,1),
        } if draw_low_275['matches'] >= 5 else {'描述': '样本不足(<5)'},
    }

# ============================================================
# THEORY 6: 平手盘4信号 (odds-based, use 1X2 to approximate)
# ============================================================
def validate_handicap_signals(c):
    """Validate handicap signals using 1X2 odds as proxy"""
    
    # Signal 1: 超低赔一方(<0.85 implied prob equivalent = odds < 1.18)
    # For 1X2: home_odds < 1.60 means implied prob > 0.85 *some* of the time
    # Better: use the actual implied probability
    results = c.execute('''
        SELECT close_home_odds, close_draw_odds, close_away_odds, final_result
        FROM historical_matches
        WHERE close_home_odds > 0 AND close_draw_odds > 0 AND close_away_odds > 0
        AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # Signal 1: best odds < 1.60 (strong favorite)
    signal1 = {'matches': 0, 'favorite_won': 0}
    for h, d, a, res in results:
        best_odds = min(h, a)
        if best_odds <= 1.60:
            signal1['matches'] += 1
            if (best_odds == h and res == 'H') or (best_odds == a and res == 'A'):
                signal1['favorite_won'] += 1
    
    # Signal 2: close odds diff < 0.3 → no bias → draw heavy
    signal3 = {'matches': 0, 'draws': 0}
    for h, d, a, res in results:
        diff = abs(h - a)
        if diff < 0.3 and 2.0 < h < 4.0 and 2.0 < a < 4.0:
            signal3['matches'] += 1
            if res == 'D':
                signal3['draws'] += 1
    
    # Signal 2: home/draw odds close → draw signal
    signal_draw_close = {'matches': 0, 'draws': 0}
    for h, d, a, res in results:
        if abs(h - d) < 0.5 and 2.0 < h < 4.0:
            signal_draw_close['matches'] += 1
            if res == 'D':
                signal_draw_close['draws'] += 1
    
    return {
        'theory': '平手盘4信号(1X2近似)',
        'signal1_超低赔': {
            '描述': '最佳赔率≤1.60 (≈0.85概率)',
            'samples': signal1['matches'],
            '打出率': round(signal1['favorite_won']/max(signal1['matches'],1)*100, 1),
            '对比_涛哥声称62%': 'PASS' if signal1['matches'] > 100 and signal1['favorite_won']/signal1['matches'] > 0.58 else 'CHECK',
        },
        'signal3_双方赔率接近': {
            '描述': '主客赔率差<0.3 (无偏向)',
            'samples': signal3['matches'],
            '平局率': round(signal3['draws']/max(signal3['matches'],1)*100, 1),
            '对比_平手盘最高平局': 'CHECK',
        },
        'signal_draw_主平赔接近': {
            '描述': '主平赔差<0.5 (2.0-4.0区间)',
            'samples': signal_draw_close['matches'],
            '平局率': round(signal_draw_close['draws']/max(signal_draw_close['matches'],1)*100, 1),
        }
    }

# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 70)
    print("哨响AI · 赔率尾数理论全量验证")
    print(f"启动时间: {datetime.now().isoformat()}")
    print("=" * 70)
    
    c = connect_hist()
    
    report = {
        'meta': {
            'timestamp': datetime.now().isoformat(),
            'data_source': 'historical_matches (football_data.db)',
            'total_rows': 312016,
        },
        'results': []
    }
    
    # Theory 1
    print("\n[1/6] 验证 1.44 死亡尾数...")
    r = validate_death_tail_144(c)
    report['results'].append(r)
    print(f"  1.44爆冷率: {r['data']['1.44_upset_rate']}% (n={r['data']['1.44_samples']}) | 判定: {r['verdict']}")
    
    # Theory 2
    print("[2/6] 验证 1.99/2.99 临界尾数...")
    r = validate_critical_tail_199(c)
    report['results'].append(r)
    print(f"  {r['verdict_detail']} | 判定: {r['verdict']}")
    
    # Theory 3
    print("[3/6] 验证 平赔尾数带8...")
    r = validate_draw_tail_8(c)
    report['results'].append(r)
    print(f"  尾8平局率: {r['data']['tail8_draw_rate']}% vs 其他: {r['data']['other_draw_rate']}% | 判定: {r['verdict']}")
    
    # Theory 4
    print("[4/6] 验证 高赔率尾数带4...")
    r = validate_high_odds_tail_4(c)
    report['results'].append(r)
    print(f"  尾4打出率: {r['data']['tail4_win_rate']}% vs 其他: {r['data']['other_win_rate']}% | 判定: {r['verdict']}")
    
    # Theory 5 (OU cross)
    print("[5/6] 验证 1X2+OU交叉验证...")
    try:
        c_fl = connect_fl()
        r = validate_1x2_ou_cross(c, c_fl)
        report['results'].append(r)
        if 'warning' in r:
            print(f"  ⚠ {r['warning']}")
        else:
            fa = r.get('formula_5a', {})
            fb = r.get('formula_5b', {})
            print(f"  5a: {fa.get('samples',0)}场 | 5b: {fb.get('samples',0)}场")
        c_fl.close()
    except Exception as e:
        report['results'].append({'theory': '1X2+OU交叉验证', 'error': str(e)})
        print(f"  ✗ {e}")
    
    # Theory 6 (Handicap signals)
    print("[6/6] 验证 平手盘信号...")
    r = validate_handicap_signals(c)
    report['results'].append(r)
    s1 = r.get('signal1_超低赔', {})
    s3 = r.get('signal3_双方赔率接近', {})
    print(f"  信号1: {s1.get('打出率',0)}% (n={s1.get('samples',0)}) | 信号3: {s3.get('平局率',0)}%")
    
    c.close()
    
    # Save report
    out_path = 'data/tail_theory_validation.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    # Summary
    print(f"\n{'='*70}")
    print(f"验证完成! 报告已保存: {out_path}")
    print(f"{'='*70}")
    
    # Summary table
    print("\n📊 验证结论总览:")
    print("-" * 70)
    for r in report['results']:
        name = r.get('theory', '?')
        v = r.get('verdict', r.get('warning', r.get('error', '?')))
        print(f"  {name:30s} → {v}")

if __name__ == '__main__':
    main()
