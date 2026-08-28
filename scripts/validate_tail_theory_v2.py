#!/usr/bin/env python3
"""
赔率尾数理论验证脚本 v2.0 — 修复版
使用区间聚类替代精确尾数匹配，适配真实赔率数据分布
"""

import sqlite3
import json
from datetime import datetime
from collections import defaultdict

def connect_hist():
    return sqlite3.connect('data/football_data.db')

def connect_fl():
    return sqlite3.connect('data/shaoxiang_feature_library.db')

def decode_odds_range():
    """First: understand what granularity the odds data has"""
    c = connect_hist()
    # Check: what values exist for close_home_odds
    sample = c.execute('''
        SELECT close_home_odds, COUNT(*) as cnt
        FROM historical_matches
        WHERE close_home_odds > 0
        GROUP BY CAST(ROUND(close_home_odds*100) AS INTEGER)
        ORDER BY cnt DESC LIMIT 30
    ''').fetchall()
    print("=== 赔率分布 Top 30 ===")
    for odds, cnt in sample:
        print(f"  {odds/100:.2f} ({int(odds)}cents) → {cnt}场")
    
    # Check the exact granularity around 1.44, 1.99, 2.00
    for target in [1.44, 1.99, 2.00, 2.99, 3.00, 3.18, 2.88]:
        nearby = c.execute('''
            SELECT CAST(ROUND(close_home_odds*100) AS INTEGER) as cents, COUNT(*)
            FROM historical_matches
            WHERE close_home_odds > 0
            AND close_home_odds BETWEEN ? AND ?
            GROUP BY cents ORDER BY cents
        ''', (target-0.02, target+0.02)).fetchall()
        print(f"\n  {target}附近: {nearby}")
    
    c.close()

# ============================================================
# THEORY 1 v2: 1.44 死亡尾数 — 用1.43-1.45区间
# ============================================================
def validate_death_tail_144_v2(c):
    """区间法验证"""
    results = c.execute('''
        SELECT close_home_odds, final_result
        FROM historical_matches
        WHERE close_home_odds > 0 AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # Target: 1.42-1.46 (centered on 1.44)
    target = {'H':0, 'D':0, 'A':0, 'total':0}
    # Compare: 1.40-1.42 + 1.46-1.50 (neighbors excluding target)
    neighbors = {'H':0, 'D':0, 'A':0, 'total':0}
    # Baseline: 1.30-1.60
    baseline = {'H':0, 'D':0, 'A':0, 'total':0}
    
    for odds, res in results:
        if 1.30 <= odds < 1.60:
            baseline[res] += 1
            baseline['total'] += 1
            if 1.42 <= odds < 1.46:
                target[res] += 1
                target['total'] += 1
            elif 1.40 <= odds < 1.42 or 1.46 <= odds < 1.50:
                neighbors[res] += 1
                neighbors['total'] += 1
    
    # Also check: home_odds 1.44 with H vs non-H
    # Check specific subsample: 1.44 only (not 1.43 or 1.45)
    exact_144 = {'H':0, 'D':0, 'A':0, 'total':0}
    exact_143 = {'H':0, 'D':0, 'A':0, 'total':0}
    exact_145 = {'H':0, 'D':0, 'A':0, 'total':0}
    for odds, res in results:
        cents = round(odds * 100)
        if cents == 144:
            exact_144[res] += 1
            exact_144['total'] += 1
        elif cents == 143:
            exact_143[res] += 1
            exact_143['total'] += 1
        elif cents == 145:
            exact_145[res] += 1
            exact_145['total'] += 1
    
    def upset_rate(d):
        return (d['D'] + d['A']) / d['total'] * 100 if d['total'] > 0 else 0
    
    return {
        'theory': '1.44死亡尾数',
        'claim': '1.44爆冷率>36%',
        'method': '区间+精确对照',
        'target_1.42_1.46': {
            'samples': target['total'],
            'upset_rate': round(upset_rate(target), 2),
            'H': target['H'], 'D': target['D'], 'A': target['A']
        },
        'neighbors_1.40_1.49_excluding_target': {
            'samples': neighbors['total'],
            'upset_rate': round(upset_rate(neighbors), 2),
        },
        'baseline_1.30_1.60': {
            'samples': baseline['total'],
            'upset_rate': round(upset_rate(baseline), 2),
        },
        'exact_compare': {
            '1.43': {'n': exact_143['total'], 'upset': round(upset_rate(exact_143),2)},
            '1.44': {'n': exact_144['total'], 'upset': round(upset_rate(exact_144),2)},
            '1.45': {'n': exact_145['total'], 'upset': round(upset_rate(exact_145),2)},
        },
        'verdict': f'1.44爆冷={upset_rate(exact_144):.1f}% vs 邻居={upset_rate(target):.1f}% vs 基线={upset_rate(baseline):.1f}%'
    }

# ============================================================
# THEORY 2 v2: 临界尾数 — 1.95-1.99 vs 2.00-2.04
# ============================================================
def validate_critical_tail_v2(c):
    """区间法: n.90-n.99 vs (n+1).00-(n+1).04"""
    results = c.execute('''
        SELECT CAST(ROUND(close_home_odds*100) AS INTEGER) as cents, 
               final_result, COUNT(*) as cnt
        FROM historical_matches
        WHERE close_home_odds > 0 AND close_home_odds < 5.0 
        AND final_result IN ('H','D','A')
        GROUP BY cents, final_result
    ''').fetchall()
    
    # Build aggregated data
    bins = defaultdict(lambda: {'H':0, 'D':0, 'A':0, 'total':0})
    for cents, res, cnt in results:
        bins[cents][res] += cnt
        bins[cents]['total'] += cnt
    
    def win_rate(lo, hi):
        """Home win rate for odds in [lo, hi] cents range"""
        h = d = a = tot = 0
        for cen in range(lo, hi+1):
            b = bins.get(cen, {})
            h += b.get('H', 0)
            d += b.get('D', 0)
            a += b.get('A', 0)
            tot += b.get('total', 0)
        return {'H': h, 'D': d, 'A': a, 'total': tot, 
                'rate': h/tot*100 if tot > 0 else 0}
    
    # Compare: n.90-n.99 vs (n+1).00-(n+1).10 for n=1,2,3
    comparisons = []
    for n in [1, 2, 3]:
        below = win_rate(n*100+90, n*100+99)  # e.g. 190-199
        above = win_rate((n+1)*100, (n+1)*100+10)  # e.g. 200-210
        diff = below['rate'] - above['rate']
        comparisons.append({
            f'{n}.90-{n}.99': {'n': below['total'], 'win_rate': round(below['rate'],2)},
            f'{n+1}.00-{n+1}.10': {'n': above['total'], 'win_rate': round(above['rate'],2)},
            'diff_pp': round(diff, 2),
            'direction': '诱盘(临界位更低)' if diff < -1 else ('反诱盘' if diff > 1 else '无差异')
        })
    
    # Also compare: away odds at 1.99 vs 2.00 (for home underdog scenario)
    # For away 1.99 vs 2.00, check if home wins more (home is dog when away odds low)
    away_results = c.execute('''
        SELECT CAST(ROUND(close_away_odds*100) AS INTEGER) as cents,
               CASE WHEN final_result='H' THEN 1 ELSE 0 END as home_won, COUNT(*) as cnt
        FROM historical_matches
        WHERE close_away_odds > 0 AND close_away_odds < 5.0
        AND final_result IN ('H','D','A')
        GROUP BY cents, home_won
    ''').fetchall()
    
    away_bins = defaultdict(lambda: {'won':0, 'total':0})
    for cents, won, cnt in away_results:
        away_bins[cents]['won'] += won * cnt
        away_bins[cents]['total'] += cnt
    
    def away_home_win_rate(lo, hi):
        won = tot = 0
        for cen in range(lo, hi+1):
            b = away_bins.get(cen, {})
            won += b.get('won', 0)
            tot += b.get('total', 0)
        return won/tot*100 if tot > 0 else 0, tot
    
    away_comp = {}
    for n in [1, 2, 3]:
        b_rate, b_n = away_home_win_rate(n*100+90, n*100+99)
        a_rate, a_n = away_home_win_rate((n+1)*100, (n+1)*100+10)
        away_comp[f'客{n}.90-{n}.99'] = {
            'home_win_rate': round(b_rate, 2), 'n': b_n,
            f'客{n+1}.00-{n+1}.10_home_win': round(a_rate, 2)
        }
    
    return {
        'theory': '1.99/2.99临界尾数(区间法)',
        'claim': 'n.99区间打出率 < (n+1).00区间 (诱盘)',
        'home_odds_comparison': comparisons,
        'away_odds_cross': away_comp,
        'verdict': 'PASS' if any(c['diff_pp'] < -1 for c in comparisons) else '需更多数据',
    }

# ============================================================
# THEORY 3 v2: 平赔尾数带8 — 用后两位=08/18/28/38/48... 
# ============================================================
def validate_draw_tail_8_v2(c):
    """Fix: properly handle cents parsing"""
    results = c.execute('''
        SELECT 
            CAST(ROUND(close_draw_odds*100) AS INTEGER) as draw_cents,
            final_result
        FROM historical_matches
        WHERE close_draw_odds > 0 
        AND close_draw_odds BETWEEN 2.0 AND 5.0
        AND final_result IN ('H','D','A')
    ''').fetchall()
    
    tail8 = {'H':0, 'D':0, 'A':0, 'total':0}
    other = {'H':0, 'D':0, 'A':0, 'total':0}
    
    for cents, res in results:
        last_digit = cents % 10
        if last_digit == 8:  # e.g. 208, 218, 228... = 2.08, 2.18, 2.28...
            tail8[res] += 1
            tail8['total'] += 1
        else:
            other[res] += 1
            other['total'] += 1
    
    # Also compare: by tier where we have enough samples
    tier_data = {}
    for tier_lo_cents in [200, 250, 300, 350, 400]:
        tier_hi_cents = tier_lo_cents + 50
        t8_tier = {'D':0, 'total':0}
        ot_tier = {'D':0, 'total':0}
        for cents, res in results:
            if tier_lo_cents <= cents < tier_hi_cents:
                if cents % 10 == 8:
                    t8_tier['total'] += 1
                    if res == 'D': t8_tier['D'] += 1
                else:
                    ot_tier['total'] += 1
                    if res == 'D': ot_tier['D'] += 1
        
        lo = tier_lo_cents/100
        hi = tier_hi_cents/100
        t8_rate = t8_tier['D']/t8_tier['total']*100 if t8_tier['total'] > 20 else 0
        ot_rate = ot_tier['D']/ot_tier['total']*100 if ot_tier['total'] > 20 else 0
        tier_data[f'{lo:.1f}-{hi:.1f}'] = {
            'tail8_n': t8_tier['total'], 'tail8_draw': round(t8_rate,2),
            'other_n': ot_tier['total'], 'other_draw': round(ot_rate,2),
            'diff_pp': round(t8_rate - ot_rate, 2)
        }
    
    t8_rate = tail8['D']/tail8['total']*100 if tail8['total'] > 0 else 0
    ot_rate = other['D']/other['total']*100 if other['total'] > 0 else 0
    
    return {
        'theory': '平赔尾数带8(区间法)',
        'claim': '平局概率比同档位高30%(相对)',
        'data': {
            'tail8_total': tail8['total'],
            'tail8_draw_rate': round(t8_rate, 2),
            'other_total': other['total'],
            'other_draw_rate': round(ot_rate, 2),
            'absolute_diff_pp': round(t8_rate - ot_rate, 2),
            'relative_diff_pct': round((t8_rate - ot_rate)/ot_rate*100,1) if ot_rate > 0 else 0,
            'tail8_breakdown': {k:tail8[k] for k in ['H','D','A']}
        },
        'by_tier': tier_data,
        'verdict': 'PASS' if t8_rate > ot_rate else ('NO_EFFECT' if abs(t8_rate - ot_rate) < 1 else 'REVERSE')
    }

# ============================================================
# THEORY 4 v2: 高赔率尾数带4
# ============================================================
def validate_high_odds_tail_4_v2(c):
    """Validate tail 4 for high underdog odds"""
    results = c.execute('''
        SELECT 
            close_home_odds, close_away_odds, final_result
        FROM historical_matches
        WHERE close_home_odds > 0 AND close_away_odds > 0
        AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # For each match, identify the underdog side and its odds
    tail4 = {'won': 0, 'total': 0}
    other = {'won': 0, 'total': 0}
    
    for h, a, res in results:
        # Determine underdog (higher odds side)
        if h > a:
            underdog_odds = h
            underdog_side = 'H'
        elif a > h:
            underdog_odds = a
            underdog_side = 'A'
        else:
            continue  # equal odds, skip
        
        if underdog_odds < 3.5:  # focus on high odds 3.5+
            continue
        
        cents = round(underdog_odds * 100)
        last_digit = cents % 10
        
        underdog_won = (underdog_side == res)
        
        if last_digit == 4:
            tail4['total'] += 1
            if underdog_won:
                tail4['won'] += 1
        else:
            other['total'] += 1
            if underdog_won:
                other['won'] += 1
    
    # Per tier
    tier_data = {}
    for lo, hi in [(3.5, 4.5), (4.5, 5.5), (5.5, 7.0)]:
        t4, ot = {'won':0,'total':0}, {'won':0,'total':0}
        for h, a, res in results:
            if h > a: ud, side = h, 'H'
            elif a > h: ud, side = a, 'A'
            else: continue
            if not (lo <= ud < hi): continue
            cents = round(ud * 100)
            ud_won = (side == res)
            if cents % 10 == 4:
                t4['total'] += 1
                if ud_won: t4['won'] += 1
            else:
                ot['total'] += 1
                if ud_won: ot['won'] += 1
        tier_data[f'{lo}-{hi}'] = {
            'tail4_n': t4['total'], 'tail4_rate': round(t4['won']/max(t4['total'],1)*100,1),
            'other_n': ot['total'], 'other_rate': round(ot['won']/max(ot['total'],1)*100,1),
        }
    
    t4_rate = tail4['won']/tail4['total']*100 if tail4['total'] > 0 else 0
    other_rate = other['won']/other['total']*100 if other['total'] > 0 else 0
    
    return {
        'theory': '高赔率尾数带4(区间法)',
        'claim': '冷门打出率比同档位高20%以上',
        'data': {
            'tail4_total': tail4['total'],
            'tail4_win_rate': round(t4_rate, 2),
            'other_total': other['total'],
            'other_win_rate': round(other_rate, 2),
            'absolute_diff_pp': round(t4_rate - other_rate, 2),
            'relative_diff_pct': round((t4_rate-other_rate)/other_rate*100,1) if other_rate > 0 else 0,
        },
        'by_tier': tier_data,
        'verdict': 'PASS' if t4_rate > other_rate + 1 else ('NO_EFFECT' if abs(t4_rate-other_rate) < 1 else 'REVERSE')
    }

# ============================================================
# THEORY 5 v2: 1X2+OU交叉验证
# ============================================================
def validate_1x2_ou_cross_v2(c_hist, c_fl):
    """Fix: use features table columns directly - xou_line, xou_over, odds_h/d/a, label_1x2, label_ou"""
    ou_data = c_fl.execute('''
        SELECT 
            league, xou_line, xou_over, xou_under, 
            label_ou, label_1x2,
            x1_h, x1_d, x1_a
        FROM features
        WHERE xou_line > 0 AND label_ou IS NOT NULL
        AND x1_h > 0
    ''').fetchall()
    
    if len(ou_data) < 100:
        return {'theory': '1X2+OU交叉验证', 'warning': f'仅{len(ou_data)}条OU数据'}
    
    print(f"  OU数据量: {len(ou_data)}")
    
    # Formula 5a: 强队低赔(≤1.55) + 2.5球 → 低进球(label_ou=0意为小)
    fa = {'total': 0, 'low_goals': 0, 'fav_wins': 0}
    for row in ou_data:
        league, xou_line, xou_over, xou_under, label_ou, label_1x2, oh, od, oa = row
        if oh is None or oh <= 0: continue
        if oh <= 1.55 and abs(xou_line - 2.5) < 0.05:
            fa['total'] += 1
            if label_ou == 0:  # under = low goals
                fa['low_goals'] += 1
            if label_1x2 == 2:  # home wins
                fa['fav_wins'] += 1
    
    # Formula 5b: 平赔3.0-3.5 + 2.75球盘 → 平局(label_1x2=1)+大球(label_ou=1)
    fb = {'total': 0, 'draws': 0, 'high_goals': 0}
    for row in ou_data:
        league, xou_line, xou_over, xou_under, label_ou, label_1x2, oh, od, oa = row
        if od is None or od <= 0: continue
        if 3.0 <= od <= 3.5 and abs(xou_line - 2.75) < 0.05:
            fb['total'] += 1
            if label_1x2 == 1:  # draw
                fb['draws'] += 1
            if label_ou == 1:  # over = high goals
                fb['high_goals'] += 1
    
    return {
        'theory': '1X2+OU交叉验证',
        'formula_5a': {
            '描述': '强队胜赔≤1.55 + OU=2.5球 → 小胜',
            'samples': fa['total'],
            '小球率(label_ou=0)': round(fa['low_goals']/max(fa['total'],1)*100, 1),
            '强队胜率(label_1x2=2)': round(fa['fav_wins']/max(fa['total'],1)*100, 1),
        } if fa['total'] >= 5 else fa,
        'formula_5b': {
            '描述': '平赔3.0-3.5 + OU=2.75球 → 平局+大球',
            'samples': fb['total'],
            '平局率': round(fb['draws']/max(fb['total'],1)*100, 1),
            '大球率≥3': round(fb['high_goals']/max(fb['total'],1)*100, 1),
        } if fb['total'] >= 5 else fb,
    }

# ============================================================
# THEORY 6 v2: 平手盘信号
# ============================================================
def validate_handicap_signals_v2(c):
    """Enhanced handicap signal validation"""
    results = c.execute('''
        SELECT close_home_odds, close_draw_odds, close_away_odds, final_result
        FROM historical_matches
        WHERE close_home_odds > 0 AND close_draw_odds > 0 AND close_away_odds > 0
        AND final_result IN ('H','D','A')
    ''').fetchall()
    
    # Signal 1: One side ≤ 1.50 (≈0.85 implied for some bookies)
    s1 = {'total':0, 'fav_won':0}
    
    # Signal 2: Odds rise by 0.10 from open to close → trap
    drift_data = c.execute('''
        SELECT open_home_odds, close_home_odds, open_away_odds, close_away_odds, final_result
        FROM historical_matches
        WHERE open_home_odds > 0 AND close_home_odds > 0
        AND open_away_odds > 0 AND close_away_odds > 0
        AND final_result IN ('H','D','A')
    ''').fetchall()
    
    s2_up = {'total':0, 'home_won':0}  # home odds up 0.10+
    s2_down = {'total':0, 'home_won':0}  # home odds down 0.10+
    
    for oh, ch, oa, ca, res in drift_data:
        drift_home = ch - oh
        if drift_home >= 0.08:  # significant rise
            s2_up['total'] += 1
            if res == 'H': s2_up['home_won'] += 1
        elif drift_home <= -0.08:  # significant drop
            s2_down['total'] += 1
            if res == 'H': s2_down['home_won'] += 1
    
    # Signal 3: Close odds (home vs away diff < 0.3) → draw heavy
    s3 = {'total':0, 'draws':0}
    s3_close = {'total':0, 'draws':0}  # diff < 0.1
    
    for h, d, a, res in results:
        # Signal 1
        if h <= 1.50 and h < a:
            s1['total'] += 1
            if res == 'H': s1['fav_won'] += 1
        elif a <= 1.50 and a < h:
            s1['total'] += 1
            if res == 'A': s1['fav_won'] += 1
        
        # Signal 3
        diff = abs(h - a)
        if diff < 0.30 and 2.0 < h < 4.5:
            s3['total'] += 1
            if res == 'D': s3['draws'] += 1
        if diff < 0.10 and 2.0 < h < 4.5:
            s3_close['total'] += 1
            if res == 'D': s3_close['draws'] += 1
    
    return {
        'theory': '平手盘+赔率信号验证',
        'signal1_超低赔≤1.50': {
            '描述': '一方赔率≤1.50(机构明确偏向)',
            'samples': s1['total'],
            '打出率': round(s1['fav_won']/max(s1['total'],1)*100, 1),
        },
        'signal2_临场升赔≥0.08': {
            '描述': '主场赔率从开盘到收盘升≥0.08',
            'samples': s2_up['total'],
            '升赔方胜率': round(s2_up['home_won']/max(s2_up['total'],1)*100, 1),
        },
        'signal2_临场降赔≥0.08': {
            '描述': '主场赔率从开盘到收盘降≥0.08',
            'samples': s2_down['total'],
            '降赔方胜率': round(s2_down['home_won']/max(s2_down['total'],1)*100, 1),
        },
        'signal3_双方赔率差<0.3': {
            '描述': '机构无明显偏向(主客赔率差<0.3)',
            'samples': s3['total'],
            '平局率': round(s3['draws']/max(s3['total'],1)*100, 1),
        },
        'signal3_双方赔率极近<0.1': {
            '描述': '机构完全中立(赔率差<0.1)',
            'samples': s3_close['total'],
            '平局率': round(s3_close['draws']/max(s3_close['total'],1)*100, 1),
        },
    }

# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 70)
    print("哨响AI · 赔率尾数理论全量验证 v2.0")
    print(f"启动: {datetime.now().isoformat()}")
    print("=" * 70)
    
    # First: understand data granularity
    decode_odds_range()
    
    c = connect_hist()
    report = {'meta': {'timestamp': datetime.now().isoformat(), 'version': 'v2.0'}, 'results': []}
    
    print("\n[1/6] 1.44死亡尾数 (区间法)...")
    r = validate_death_tail_144_v2(c)
    report['results'].append(r)
    print(f"  → {r['verdict']}")
    
    print("[2/6] 1.99/2.99临界尾数 (区间法)...")
    r = validate_critical_tail_v2(c)
    report['results'].append(r)
    for comp in r.get('home_odds_comparison', []):
        print(f"  → {comp}")
    
    print("[3/6] 平赔尾数带8 (修复)...")
    r = validate_draw_tail_8_v2(c)
    report['results'].append(r)
    print(f"  → tail8_total={r['data']['tail8_total']} draw_rate={r['data']['tail8_draw_rate']}% vs other={r['data']['other_draw_rate']}%")
    
    print("[4/6] 高赔率尾数带4 (修复)...")
    r = validate_high_odds_tail_4_v2(c)
    report['results'].append(r)
    print(f"  → tail4_total={r['data']['tail4_total']} win_rate={r['data']['tail4_win_rate']}% vs other={r['data']['other_win_rate']}%")
    
    print("[5/6] 1X2+OU交叉验证...")
    try:
        c_fl = connect_fl()
        r = validate_1x2_ou_cross_v2(c, c_fl)
        report['results'].append(r)
        c_fl.close()
    except Exception as e:
        report['results'].append({'theory': '1X2+OU交叉验证', 'error': str(e)})
        print(f"  ✗ {e}")
    
    print("[6/6] 平手盘+赔率漂移信号...")
    r = validate_handicap_signals_v2(c)
    report['results'].append(r)
    for k, v in r.items():
        if isinstance(v, dict) and 'samples' in v:
            print(f"  {k}: n={v['samples']} rate={v.get(list(v.keys())[-1],'?')}%")
    
    c.close()
    
    # Save
    out_path = 'data/tail_theory_validation_v2.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"\n验证完成! 报告: {out_path}")

if __name__ == '__main__':
    main()
