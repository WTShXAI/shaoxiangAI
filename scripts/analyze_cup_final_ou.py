"""验证假设: 杯赛/决赛场景的进球是否系统性低于盘口去水隐含概率(联赛校准),
导致基于去水锚的 OU '看大'方向在决赛系统性失利。

铁律约束: 只用去水不变量(隐含概率), 不碰原始赔率值。
"""
import sqlite3, math, json
from collections import defaultdict

DB = r'D:/Architecture/data/events.db'

CUP_KEYS = ['杯', '决赛', 'Cup', 'Final', '天皇', '联赛杯']

def is_cup(league: str) -> bool:
    if not league:
        return False
    return any(k.lower() in league.lower() for k in CUP_KEYS)

def dewater(over_odds, under_odds):
    """去水隐含 P(total > line)"""
    if not over_odds or not under_odds:
        return None
    p_o = 1.0 / over_odds
    p_u = 1.0 / under_odds
    s = p_o + p_u
    if s <= 0:
        return None
    return p_o / s  # 去水 P(over)

def main():
    c = sqlite3.connect(DB)
    cur = c.cursor()

    # 1) 取所有 finished 且有终比分的比赛
    cur.execute("""SELECT match_key, league, score_home, score_away, kickoff
                   FROM matches WHERE status='finished'
                   AND score_home IS NOT NULL AND score_away IS NOT NULL""")
    rows = cur.fetchall()
    print(f"[info] finished 有终比分场数 = {len(rows)}")

    # 2) 取每场赛前 OU 盘口(最早 captured_at 的一条非1H/2H OU)
    #    优先主盘 2.50/2.75/3.00
    cur.execute("""SELECT match_key, market, selection, odds, line, captured_at
                   FROM odds_snapshots
                   WHERE market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'""")
    snaps = cur.fetchall()
    # 组织: match_key -> list of (cap, line, sel, odds, market)
    by_match = defaultdict(list)
    for mk, mkt, sel, odds, line, cap in snaps:
        by_match[mk].append((cap, line, sel, odds, mkt))
    print(f"[info] 有 OU 快照的 (match_key,market) 组 = {len(by_match)}")

    PRIORITY = {'OU_2.50': 0, 'OU_2.75': 1, 'OU_3.00': 2, 'OU_2.25': 3, 'OU_3.25': 4}
    def pick_line(entries):
        # 按 priority 选一条主盘 (over+under 成对)
        cand = {}
        for cap, line, sel, odds, mkt in entries:
            cand.setdefault(mkt, []).append((cap, sel, odds, line))
        # 找最优先且有 over+under 成对的
        for mkt in sorted(cand.keys(), key=lambda m: PRIORITY.get(m, 99)):
            ov = un = None
            for cap, sel, odds, line in sorted(cand[mkt]):
                if sel.lower() in ('over', '大'): ov = odds
                elif sel.lower() in ('under', '小'): un = odds
            if ov and un:
                return line, ov, un
        return None

    # 3) 构建分析样本
    cup_stats = {'n': 0, 'totals': [], 'p_over': [], 'market_over_actual_over': [], 'actual_over_when_mkt_over': []}
    noncup_stats = {'n': 0, 'totals': [], 'p_over': [], 'market_over_actual_over': [], 'actual_over_when_mkt_over': []}
    today = '2026-08-19'
    today_cup = {'n':0,'totals':[], 'p_over':[], 'act_over':[]}
    today_noncup = {'n':0,'totals':[], 'p_over':[], 'act_over':[]}

    for mk, league, sh, sa, ko in rows:
        entries = by_match.get(mk)
        if not entries:
            continue
        picked = pick_line(entries)
        if not picked:
            continue
        line, ov, un = picked
        p_over = dewater(ov, un)
        if p_over is None:
            continue
        total = (sh or 0) + (sa or 0)
        actual_over = 1 if total > line else (0 if total < line else None)  # None=走盘
        if actual_over is None:
            continue
        grp = cup_stats if is_cup(league) else noncup_stats
        grp['n'] += 1
        grp['totals'].append(total)
        grp['p_over'].append(p_over)
        grp['market_over_actual_over'].append((p_over, actual_over))
        if p_over >= 0.5:
            grp['actual_over_when_mkt_over'].append(actual_over)
        # 今天子集
        if ko and today in str(ko):
            tg = today_cup if is_cup(league) else today_noncup
            tg['n'] += 1
            tg['totals'].append(total)
            tg['p_over'].append(p_over)
            tg['act_over'].append(actual_over)

    def summ(name, s):
        if s['n'] == 0:
            print(f"\n=== {name}: 无样本 ===")
            return
        n = s['n']
        mean_total = sum(s['totals'])/n
        mean_pover = sum(s['p_over'])/n
        # 实际 over 频率
        act_over_freq = sum(s['market_over_actual_over'][:,1] if False else [a for _,a in s['market_over_actual_over']]) / n
        # 市场看好大球(p_over>=0.5)时, 实际 over 频率
        w = s['actual_over_when_mkt_over']
        w_freq = (sum(w)/len(w)) if w else float('nan')
        print(f"\n=== {name} (n={n}) ===")
        print(f"  实际总球均值      = {mean_total:.3f}")
        print(f"  市场去水 P(over)  = {mean_pover:.3f}  (隐含期望总球 ≈ 线 + 偏差)")
        print(f"  实际 over 总频率  = {act_over_freq:.3f}")
        print(f"  市场看大(p>=0.5)时 实际over频率 = {w_freq:.3f}  (若<<0.5 → 市场系统性高估大球)")

    summ("杯赛/决赛", cup_stats)
    summ("非杯赛(联赛等)", noncup_stats)
    summ(f"今天({today})杯赛", today_cup)
    summ(f"今天({today})非杯赛", today_noncup)

    # 额外: 杯赛 vs 非杯赛 实际总球 t检验近似(均值差)
    print("\n=== 杯赛 vs 非杯赛 实际总球均值差 ===")
    if cup_stats['totals'] and noncup_stats['totals']:
        c_mean = sum(cup_stats['totals'])/len(cup_stats['totals'])
        n_mean = sum(noncup_stats['totals'])/len(noncup_stats['totals'])
        print(f"  杯赛均值 {c_mean:.3f}  vs  非杯赛均值 {n_mean:.3f}  → 差 {c_mean-n_mean:+.3f}")

if __name__ == '__main__':
    main()
