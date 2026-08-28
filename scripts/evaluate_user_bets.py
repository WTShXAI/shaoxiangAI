#!/usr/bin/env python3
"""
evaluate_user_bets.py — 按市场类型/联赛/赔率区间/玩法 汇总 ROI,
识别 +EV / -EV 盘口特征 → 作为模型训练目标(标签)或 +EV 过滤器特征.

用法:
  python scripts/evaluate_user_bets.py
"""
import sqlite3, os, sys
from collections import defaultdict

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'events.db')


def bucket_odds(o):
    """赔率分桶: 低水(1.01-1.5)/中低(1.5-2)/中(2-3)/中高(3-5)/高赔(5-10)/超高(10-30)/极冷(30+)."""
    if o is None: return 'unknown'
    if o < 1.5: return '1_低水'
    if o < 2.0: return '2_中低'
    if o < 3.0: return '3_中'
    if o < 5.0: return '4_中高'
    if o < 10.0: return '5_高赔'
    if o < 30.0: return '6_超高'
    return '7_极冷'


def bucket_league(lg):
    """联赛分桶: 主流(5大/U19/欧战/WC) / obscure / 其他."""
    main_kw = ['西甲', '英超', '德甲', '意甲', '法甲', '中超', '亚冠', '世界杯', '欧', 'UEFA', 'U19', 'U23',
               '天皇杯', 'MLS', 'J2', 'K联赛', 'K1', 'K2', '阿甲', '巴甲', '墨超', '沙特', '葡超', '荷甲',
               '英冠', '西乙', '意乙', '德乙', '日联', '韩K', '澳A', '中超', '美职业', '美职']
    if not lg: return 'unknown'
    if any(k in lg for k in main_kw): return '主流'
    return 'obscure'


def summarize(rows, group_key):
    """按 group_key 分组汇总 (stake/pnl/win/loss/ROI/命中率)."""
    groups = defaultdict(lambda: dict(stake=0.0, pnl=0.0, n=0, wins=0))
    for r in rows:
        g = group_key(r)
        groups[g]['stake'] += r['stake']
        groups[g]['pnl'] += r['pnl']
        groups[g]['n'] += 1
        if r['result'] == 'win':
            groups[g]['wins'] += 1
    rows_out = []
    for g, d in groups.items():
        roi = d['pnl'] / d['stake'] * 100 if d['stake'] else 0
        hit = d['wins'] / d['n'] * 100 if d['n'] else 0
        rows_out.append((g, d['n'], d['wins'], d['stake'], d['pnl'], roi, hit))
    return sorted(rows_out, key=lambda x: x[5], reverse=True)  # 按 ROI 降序


def print_table(title, rows):
    print(f'\n=== {title} ===')
    print(f'{"分组":40s} {"n":>4s} {"赢":>4s} {"投注额":>10s} {"净输赢":>10s} {"ROI%":>8s} {"命中率%":>8s}')
    for g, n, w, st, pnl, roi, hit in rows:
        print(f'{g[:40]:40s} {n:4d} {w:4d} {st:10.2f} {pnl:+10.2f} {roi:+8.1f} {hit:8.1f}')


if __name__ == '__main__':
    c = sqlite3.connect(DB, timeout=30)
    rows = [dict(r) for r in c.execute("SELECT * FROM user_bets").fetchall()]
    c.close()
    if not rows:
        print('user_bets 表空. 先跑: python scripts/ingest_user_bets.py <file>')
        sys.exit(1)
    print(f'数据: {len(rows)} 单 (已过滤: 投注成功 且 pnl≠0)')

    # 总览
    total_stake = sum(r['stake'] for r in rows)
    total_pnl = sum(r['pnl'] for r in rows)
    wins = [r for r in rows if r['result'] == 'win']
    losses = [r for r in rows if r['result'] == 'loss']
    print(f'\n=== 总览 ===')
    print(f'投注额: {total_stake:.2f} | 净输赢: {total_pnl:+.2f} | ROI: {total_pnl/total_stake*100:+.1f}%')
    print(f'命中率: {len(wins)}/{len(rows)} = {len(wins)/len(rows)*100:.1f}% | 净赢平均: {(sum(r["pnl"] for r in wins)/len(wins)):.2f} | 净输平均: {(sum(r["pnl"] for r in losses)/len(losses)):.2f}')

    # 按市场类型
    print_table('按 玩法/市场 类型 ROI', summarize(rows, lambda r: r['market_type'] or 'unknown'))
    # 按赔率区间
    print_table('按 赔率区间 ROI', summarize(rows, lambda r: bucket_odds(r['odds'])))
    # 按联赛类型(主流/obscure)
    print_table('按 联赛类型 ROI', summarize(rows, lambda r: bucket_league(r['league'])))
    # 按 phase(滚球/赛前)
    print_table('按 phase(滚球/赛前) ROI', summarize(rows, lambda r: r['phase'] or 'unknown'))
    # 玩法 × 赔率区间 交叉
    cross = defaultdict(lambda: dict(stake=0, pnl=0, n=0))
    for r in rows:
        k = f'{r["market_type"][:12]}/{bucket_odds(r["odds"])}'
        cross[k]['stake'] += r['stake']
        cross[k]['pnl'] += r['pnl']
        cross[k]['n'] += 1
    print(f'\n=== 玩法 × 赔率区间 交叉 (Top ROI) ===')
    rows_sorted = sorted(cross.items(), key=lambda x: x[1]['pnl']/x[1]['stake'] if x[1]['stake'] else 0, reverse=True)
    print(f'{"组合":35s} {"n":>4s} {"投注额":>10s} {"净输赢":>10s} {"ROI%":>8s}')
    for k, d in rows_sorted[:15]:
        roi = d['pnl']/d['stake']*100 if d['stake'] else 0
        print(f'{k[:35]:35s} {d["n"]:4d} {d["stake"]:10.2f} {d["pnl"]:+10.2f} {roi:+8.1f}')