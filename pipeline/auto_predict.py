#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AutoPredict — 一键全自动预测管线
=================================
用法:
    python pipeline/auto_predict.py 6.25
    python pipeline/auto_predict.py 6.25-6.28
    python pipeline/auto_predict.py today

流程:
  1. 自动更新积分榜
  2. 自动获取赛程
  3. 自动抓取赔率
  4. 运行 D-Gate + Tournament Dynamics + 比分预测
  5. 输出完整报告

插件架构: 所有组件通过注册机制接入，新增功能自动生效。
"""

import sys, os, json, argparse
from pathlib import Path
from collections import defaultdict

# Ensure project root in path
PROJECT_ROOT = Path(__file__).parent.parent

# 插件注册表
# ═══════════════════════════════════════

PLUGINS = []

def register_plugin(name, fn, priority=50):
    """注册一个预测插件。priority越小越先执行。"""
    PLUGINS.append({'name': name, 'fn': fn, 'priority': priority})
    PLUGINS.sort(key=lambda x: x['priority'])

# ═══════════════════════════════════════
# 核心插件
# ═══════════════════════════════════════

def plugin_load_standings(matches, standings, matchday):
    """P1: 加载积分榜"""
    from data_collector.standings_updater import update_standings, get_current_matchday
    
    fresh = update_standings()
    if fresh:
        standings.update(fresh)
    
    # Auto-detect matchday from first match date
    if matchday is None and matches:
        md = get_current_matchday(matches[0][0])
        return matches, standings, md
    
    return matches, standings, matchday

def plugin_fetch_odds(matches, standings, matchday):
    """P2: 获取赔率"""
    from data_collector.odds_fetcher import get_all_odds
    
    # Extract home/away/date from matches
    odds_input = [(h, a, dt) for dt, h, a in matches]
    odds_result = get_all_odds(odds_input)
    
    # Merge back
    result = []
    for (dt, h, a), (_, _, oh, od, oa, hcp, ou) in zip(matches, odds_result):
        result.append((dt, h, a, oh, od, oa, hcp, ou))
    
    return result, standings, matchday

def plugin_predict(matches_with_odds, standings, matchday):
    """P3: 运行预测"""
    from rules.tournament_dynamics import predict_with_scores
    
    results = []
    for dt, h, a, oh, od, oa, hcp, ou in matches_with_odds:
        # Odds → implied probabilities
        rh, rd, ra = 1/oh, 1/od, 1/oa
        margin = rh + rd + ra
        ph, pd, pa = rh/margin, rd/margin, ra/margin
        
        # Get group table
        h_grp = standings.get(h, {}).get('group', '?')
        a_grp = standings.get(a, {}).get('group', '?')
        
        # Build group table from standings
        group_table = {}
        if h_grp != '?' and a_grp != '?':
            for team, info in standings.items():
                if info.get('group') in (h_grp, a_grp):
                    group_table[team] = info
        
        r = predict_with_scores(ph, pd, pa, oh, od, oa, hcp, ou, h, a,
                                 group_table=group_table if group_table else None,
                                 matchday=matchday)
        
        # Flatten for output
        results.append({
            'date': dt,
            'home': h,
            'away': a,
            'odds': f'{oh}/{od}/{oa}',
            'hcp': hcp,
            'ou': ou,
            'verdict': r['verdict'],
            'winner': r['winner'],
            'mode': r['mode'],
            'scores': r['scores'],
            'signals': r['signals'],
            'lambda_h': r['lambda_h'],
            'lambda_a': r['lambda_a'],
        })
    
    return results, standings, matchday

def plugin_risk_assess(results, standings, matchday):
    """P4: 风险评估"""
    for r in results:
        risks = []
        
        # Mode C detection
        if r['verdict'] == 'D':
            parts = r['odds'].split('/')
            if float(parts[0]) <= 1.30:
                risks.append('🔴 Mode C: 超级热门翻车预警')
            else:
                risks.append('🟡 平局预警')
        
        # R1 rotation risk
        for s in r['signals']:
            if '已出线→可能轮换' in s:
                risks.append('🟡 强队轮换风险')
            elif '已淘汰' in s:
                risks.append('⚪ 弱队动力不足')
        
        # Narrow spread
        ph = (1/float(r['odds'].split('/')[0])) / (1/float(r['odds'].split('/')[0]) + 1/float(r['odds'].split('/')[1]) + 1/float(r['odds'].split('/')[2]))
        pa = (1/float(r['odds'].split('/')[2])) / (1/float(r['odds'].split('/')[0]) + 1/float(r['odds'].split('/')[1]) + 1/float(r['odds'].split('/')[2]))
        if abs(ph - pa) < 0.15 and not risks:
            risks.append('🟡 窄spread旗鼓相当')
        
        if not risks:
            risks.append('⚪ 安全')
        
        r['risks'] = risks
    
    return results, standings, matchday

# ═══════════════════════════════════════
# 插件注册
# ═══════════════════════════════════════

register_plugin('load_standings', plugin_load_standings, 10)
register_plugin('fetch_odds', plugin_fetch_odds, 20)
register_plugin('predict', plugin_predict, 30)
register_plugin('risk_assess', plugin_risk_assess, 40)

# ═══════════════════════════════════════
# 主入口
# ═══════════════════════════════════════

def get_schedule(start_date, end_date=None):
    """从数据库获取赛程"""
    # Normalize dates: '6.25' → '2026-06-25'
    def norm(dt):
        m, d = dt.split('.')
        return f'2026-{int(m):02d}-{int(d):02d}'
    
    db_path = PROJECT_ROOT / 'data' / 'wc2026_timeline.db'
    if not db_path.exists():
        return _get_fallback_schedule(start_date, end_date)
    
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    
    s = norm(start_date)
    e = norm(end_date) if end_date else s
    
    cur.execute("""
        SELECT match_date, home_team, away_team
        FROM wc2026_matches
        WHERE match_date BETWEEN ? AND ?
        ORDER BY match_date
    """, (s, e))
    
    rows = cur.fetchall()
    conn.close()
    
    # Format dates back
    result = []
    for dt, h, a in rows:
        date_short = dt[5:].replace('-', '.')  # '2026-06-25' → '6.25'
        result.append((date_short, h, a))
    
    return result or _get_fallback_schedule(start_date, end_date)

def _get_fallback_schedule(start_date, end_date=None):
    """Fallback schedule (when DB not available)"""
    # Complete schedule for 6.25-6.28
    full = {
        '6.25': [
            ('南非','韩国'), ('捷克','墨西哥'),
            ('摩洛哥','海地'), ('波黑','卡塔尔'),
            ('瑞士','加拿大'), ('苏格兰','巴西'),
        ],
        '6.26': [
            ('厄瓜多尔','德国'), ('土耳其','美国'),
            ('巴拉圭','澳大利亚'), ('库拉索','科特迪瓦'),
            ('日本','瑞典'), ('突尼斯','荷兰'),
        ],
        '6.27': [
            ('乌拉圭','西班牙'), ('佛得角共和国','沙特阿拉伯'),
            ('埃及','伊朗'), ('塞内加尔','伊拉克'),
            ('挪威','法国'), ('新西兰','比利时'),
        ],
        '6.28': [
            ('克罗地亚','加纳'), ('哥伦比亚','葡萄牙'),
            ('巴拿马','英格兰'), ('民主刚果','乌兹别克斯坦'),
            ('约旦','阿根廷'), ('阿尔及利亚','奥地利'),
        ],
    }
    
    result = []
    for date in sorted(full.keys()):
        if date == start_date or (end_date and start_date <= date <= end_date):
            for h, a in full[date]:
                result.append((date, h, a))
    return result

def predict_date(date_str):
    """一键预测某一天的比赛"""
    return predict_range(date_str, date_str)

def predict_range(start_str, end_str=None):
    """一键预测日期范围内的所有比赛"""
    # Step 0: Get schedule
    matches = get_schedule(start_str, end_str or start_str)
    if not matches:
        print(f"未找到 {start_str} 的比赛安排")
        return []
    
    # Initialize state
    standings = {}
    matchday = None
    
    # Run pipeline
    state = (matches, standings, matchday)
    results = None
    
    for plugin in PLUGINS:
        try:
            if plugin['name'] == 'predict':
                state = plugin['fn'](state[0], state[1], state[2])
                results = state[0]
            else:
                state = plugin['fn'](state[0], state[1], state[2])
        except Exception as e:
            print(f"  [{plugin['name']}] 失败: {e}")
    
    return results

def format_report(results):
    """格式化输出预测报告"""
    if not results:
        return "无预测结果"
    
    lines = []
    lines.append("=" * 80)
    lines.append(f"哨响AI v5.2.14 全自动预测  ({len(results)}场)")
    lines.append("=" * 80)
    
    # Group by date
    by_date = defaultdict(list)
    for r in results:
        by_date[r['date']].append(r)
    
    for date in sorted(by_date.keys()):
        matches = by_date[date]
        lines.append(f"\n── {date} ({len(matches)}场) ──")
        lines.append(f"{'比赛':<28s}{'赔率':<18s}{'判型':<6s}{'预测':<12s}{'比分':<14s}{'风险'}")
        lines.append("-" * 95)
        
        for r in matches:
            sc = r['scores']
            s1 = f'{sc[0][0]}-{sc[0][1]}'
            s2 = f'{sc[1][0]}-{sc[1][1]}'
            risk = r['risks'][0] if r['risks'] else '-'
            lines.append(
                f'{r["home"][:8]+"vs"+r["away"][:8]:<28s}'
                f'{r["odds"]:<18s}'
                f'{r["verdict"]:<6s}'
                f'{r["winner"]:<12s}'
                f'{s1}/{s2:<8s}'
                f'{risk}'
            )
    
    # Summary
    critical = sum(1 for r in results if any('🔴' in ri for ri in r.get('risks', [])))
    warning = sum(1 for r in results if any('🟡' in ri for ri in r.get('risks', [])))
    safe = len(results) - critical - warning
    
    lines.append(f"\n{'='*80}")
    lines.append(f"风险评估: 🔴{critical}高危  🟡{warning}预警  ⚪{safe}安全")
    lines.append(f"{'='*80}")
    
    return '\n'.join(lines)

# ═══════════════════════════════════════
# CLI
# ═══════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='哨响AI 一键预测')
    parser.add_argument('date', help='日期，如 6.25 或 6.25-6.28 或 today')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    args = parser.parse_args()
    
    date_str = args.date
    
    if date_str == 'today':
        import datetime
        now = datetime.datetime.now(timezone.utc)
        date_str = f'{now.month}.{now.day}'
    
    if '-' in date_str:
        parts = date_str.split('-')
        results = predict_range(parts[0], parts[1])
    else:
        results = predict_date(date_str)
    
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(format_report(results))
