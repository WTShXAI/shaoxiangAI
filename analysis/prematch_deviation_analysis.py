# -*- coding: utf-8 -*-
"""
赛前偏差分析 (Pre-kickoff Deviation Analysis)
==============================================
验证涛哥提出的庄家操盘三层人性偏差假设，全部基于真实数据、可证伪、不伪造。

假设 H1: 1X2 隐藏强队战力 —— 庄家在开盘赔率中压低强队隐含概率(抬高赔率),
         强队打出正常比分时高赔率(= 开盘被低估, 赛前被市场发现而"变短")。
         测法: historical_matches 31万场, 比较 favorite 开盘隐含概率 vs 实际胜率,
              并按 开盘→即时 漂移方向分桶(变短=开盘被低估)。

假设 H2: 上半场杀大球 / 下半场杀小球 / 打穿初盘大球 ——
         1H 总进球打穿 OU_1H 线(上半场大球)后, 全场总进球 stick 在初盘大球线下方(全场小球)
         的概率是否显著高于基准。
         测法: GQ 同时有 OU_1H + 全场 OU 盘口且带半场比分的比赛(~271场), 条件概率。

假设 H3: 首个进球后盘口归一化(赛前偏差可回收) ——
         我们库无滚球 live 1X2 历史(仅3行 inplay), 故用 开盘→即时(open→close)漂移 作代理:
         若开盘线嵌操纵、赛前尾声"归一", 则 开盘→即时 漂移方向应可预测赛果。
         与 H1 同源(开盘异常、收盘正常、漂移方向预测结果)。

铁律: 缺数据标 -- 不伪造; 样本偏小的结论明确标注"方向性/非部署级"。
"""
import sqlite3, json, math
from collections import defaultdict

HIST = "D:/Architecture/data/football_data.db"
GQ   = "D:/Architecture/data/events.db"

def margin_strip(odds3):
    s = sum(1.0/x for x in odds3)
    return [(1.0/x)/s for x in odds3]

# ───────────────────────── H1 ─────────────────────────
def analyze_h1():
    f = sqlite3.connect(HIST)
    rows = f.execute("""
        SELECT open_home_odds,open_draw_odds,open_away_odds,
               close_home_odds,close_draw_odds,close_away_odds,
               home_score,away_score
        FROM historical_matches
        WHERE open_home_odds>1.01 AND open_draw_odds>1.01 AND open_away_odds>1.01
          AND close_home_odds>1.01 AND close_draw_odds>1.01 AND close_away_odds>1.01
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """).fetchall()
    f.close()

    # 桶: favorite 开盘赔率区间
    bands = [(1.20,1.40),(1.40,1.70),(1.70,2.20),(2.20,3.50),(3.50,99)]
    def band_of(o):
        for lo,hi in bands:
            if lo<=o<hi: return f"{lo}-{hi}"
        return None

    # 聚合
    agg = defaultdict(lambda: dict(n=0,wins=0,open_stake=0.0,open_ret=0.0,
                                   p_open_sum=0.0, p_close_sum=0.0,
                                   shorter_n=0,shorter_wins=0,shorter_stake=0.0,shorter_ret=0.0,
                                   longer_n=0,longer_wins=0,longer_stake=0.0,longer_ret=0.0))
    overall = dict(n=0,wins=0,open_stake=0,open_ret=0,p_open_sum=0,p_close_sum=0,open_fav_sum=0,
                   shorter_n=0,shorter_wins=0,shorter_stake=0,shorter_ret=0,shorter_fav_sum=0,
                   longer_n=0,longer_wins=0,longer_stake=0,longer_ret=0,longer_fav_sum=0)

    for oh,od,oa,ch,cd,ca,hs,as_ in rows:
        open_o=(oh,od,oa); close_o=(ch,cd,ca)
        fi = open_o.index(min(open_o))           # 开盘 favorite 下标
        open_fav = open_o[fi]; close_fav = close_o[fi]
        # 实际胜负 (draw=0.5 用于胜率, 但 ROI 用 win/lose)
        if fi==0:   win = 1 if hs>as_ else (0.5 if hs==as_ else 0); won = hs>as_
        elif fi==2: win = 1 if as_>hs else (0.5 if hs==as_ else 0); won = as_>hs
        else:       win = 0.5 if hs==as_ else 0;                    won = False  # draw favorite 不存在, 跳过 ROI 下注
        p_open = margin_strip(open_o)[fi]
        p_close = margin_strip(close_o)[fi]
        b = band_of(open_fav)

        def add(d, won_bool, open_fav_odds, p_o, p_c, winrate):
            d['n']+=1; d['wins']+=winrate; d['p_open_sum']+=p_o; d['p_close_sum']+=p_c
            if open_fav_odds>=1.01:
                d['open_stake']+=1.0; d['open_ret']+= (open_fav_odds if won_bool else 0.0)
        # 全局
        overall['n']+=1; overall['wins']+=win; overall['p_open_sum']+=p_open; overall['p_close_sum']+=p_close
        overall['open_stake']+=1.0; overall['open_ret']+=(open_fav if won else 0.0); overall['open_fav_sum']+=open_fav
        if b: add(agg[b], won, open_fav, p_open, p_close, win)
        # 漂移分桶 (开盘 vs 即时 favorite 赔率)
        if open_fav > close_fav:   # favorite 变短 = 开盘被低估(隐藏战力)
            overall['shorter_n']+=1; overall['shorter_wins']+=win
            overall['shorter_stake']+=1.0; overall['shorter_ret']+=(open_fav if won else 0.0); overall['shorter_fav_sum']+=open_fav
            if b:
                agg[b]['shorter_n']+=1; agg[b]['shorter_wins']+=win
                agg[b]['shorter_stake']+=1.0; agg[b]['shorter_ret']+=(open_fav if won else 0.0)
        else:                      # favorite 变长 = 开盘被高估
            overall['longer_n']+=1; overall['longer_wins']+=win
            overall['longer_stake']+=1.0; overall['longer_ret']+=(open_fav if won else 0.0); overall['longer_fav_sum']+=open_fav
            if b:
                agg[b]['longer_n']+=1; agg[b]['longer_wins']+=win
                agg[b]['longer_stake']+=1.0; agg[b]['longer_ret']+=(open_fav if won else 0.0)

    def roi(d, prefix=''):
        st=d.get((prefix+'_stake') if prefix else 'open_stake')
        ret=d.get((prefix+'_ret') if prefix else 'open_ret')
        return (ret/st - 1.0) if st and st>0 else None
    def winrate(d, prefix=''):
        n=d.get((prefix+'_n') if prefix else 'n')
        w=d.get((prefix+'_wins') if prefix else 'wins')
        return (w/n) if n else None

    out = {'total_matches': overall['n'],
           'fav_actual_winrate_all': winrate(overall) ,
           'fav_open_implied_all': overall['p_open_sum']/overall['n'],
           'fav_close_implied_all': overall['p_close_sum']/overall['n'],
           'roi_bet_all_favorites_at_open': roi(overall),
           'avg_open_fav_odds_all': overall['open_fav_sum']/overall['n'] if overall['n'] else None,
           'drift_shorter': {'n':overall['shorter_n'],
                             'winrate':winrate(overall,'shorter'),
                             'avg_open_fav_odds':overall['shorter_fav_sum']/overall['shorter_n'] if overall['shorter_n'] else None,
                             'roi_bet_fav_at_open_when_shortened': roi(overall,'shorter')},
           'drift_longer': {'n':overall['longer_n'],
                            'winrate':winrate(overall,'longer'),
                            'avg_open_fav_odds':overall['longer_fav_sum']/overall['longer_n'] if overall['longer_n'] else None,
                            'roi_bet_fav_at_open_when_lengthened': roi(overall,'longer')},
           'by_band': {}}
    for b in [f"{lo}-{hi}" for lo,hi in bands]:
        if b not in agg: continue
        d=agg[b]
        out['by_band'][b]={
            'n':d['n'],
            'fav_actual_winrate':winrate(d),
            'fav_open_implied':d['p_open_sum']/d['n'],
            'roi_bet_all_fav_at_open':roi(d),
            'shorter_n':d['shorter_n'],
            'shorter_winrate':winrate(d,'shorter'),
            'shorter_roi':roi(d,'shorter'),
            'longer_n':d['longer_n'],
            'longer_winrate':winrate(d,'longer'),
            'longer_roi':roi(d,'longer'),
        }
    return out

# ───────────────────────── H2 ─────────────────────────
def parse_ou(market):
    try:
        if market.startswith('OU_1H_'):
            return ('1H', float(market[6:]))
        if market.startswith('OU_') and not market.startswith('OU_1H') and not market.startswith('OU_2H'):
            return ('F', float(market[3:]))
    except: return None
    return None

def analyze_h2():
    g = sqlite3.connect(GQ)
    g.row_factory = sqlite3.Row
    # 带半场比分的 finished 比赛
    # HT 污染清洗 (SSoT: 仅 ht_total < ft_total 的半场真值可信, 见 gq/db.py HT_CLEAN_RULE)
    keys = g.execute("""SELECT home,away,ht_score_home,ht_score_away,score_home,score_away
        FROM matches WHERE status='finished'
          AND score_home IS NOT NULL AND score_away IS NOT NULL
          AND ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
          AND (ht_score_home + ht_score_away) < (score_home + score_away)""").fetchall()
    # 取这些 match_key 的 OU 盘口线
    rec = {}
    for r in keys:
        mk = f"{r['home']} vs {r['away']}"
        rec[mk] = dict(ht=r['ht_score_home']+r['ht_score_away'],
                       ft=r['score_home']+r['score_away'],
                       ou1h=None, ouf=None)
    # 拉 OU 盘口 (只取这些 key)
    mks = list(rec.keys())
    # 分批查避免过长 IN
    snaps = defaultdict(dict)  # match_key -> market_line
    for i in range(0,len(mks),500):
        batch = mks[i:i+500]
        q = "SELECT match_key,market,line FROM odds_snapshots WHERE market LIKE 'OU%' AND match_key IN ({})".format(
            ",".join("?"*len(batch)))
        for row in g.execute(q, batch):
            mk=row['match_key']; p=parse_ou(row['market'])
            if not p: continue
            if p[0]=='1H' and rec.get(mk) and rec[mk]['ou1h'] is None:
                rec[mk]['ou1h']=p[1]
            elif p[0]=='F' and rec.get(mk) and rec[mk]['ouf'] is None:
                rec[mk]['ouf']=p[1]
    g.close()

    n_both=0
    base_under=0; base_n=0
    cond_under=0; cond_n=0          # 1H over -> full under
    cond_over=0                     # 1H over -> full over (对照)
    cond_under_given_over_base=0
    for mk,d in rec.items():
        if d['ou1h'] is None or d['ouf'] is None: continue
        n_both+=1
        over_1h = d['ht'] > d['ou1h']
        under_full = d['ft'] < d['ouf']
        over_full = d['ft'] > d['ouf']
        base_n+=1; base_under += (1 if under_full else 0)
        if over_1h:
            cond_n+=1
            cond_under += (1 if under_full else 0)
            cond_over += (1 if over_full else 0)
    return {
        'matches_with_both_lines': n_both,
        'base_rate_full_under': (base_under/base_n) if base_n else None,
        'given_1h_over_n': cond_n,
        'given_1h_over_full_under': (cond_under/cond_n) if cond_n else None,
        'given_1h_over_full_over': (cond_over/cond_n) if cond_n else None,
        'lift': ((cond_under/cond_n) - (base_under/base_n)) if (cond_n and base_n) else None,
        'note': '样本小(~271), 仅方向性; 用 opening line 解析自 market 名; 严格 > 判定 over'
    }

# ───────────────────────── H3 (代理) ─────────────────────────
def analyze_h3_proxy(h1):
    # H3 用 H1 的 open->close 漂移代理: 漂移方向预测赛果 = 开盘异常可被"赛前归一"回收
    # 同时报告: 开盘 favorite 被低估(变短)时, 其 open 隐含 vs close 隐含 的偏差幅度
    s = h1['drift_shorter']; l = h1['drift_longer']
    return {
        'proxy_note': '本库无滚球 live 1X2 历史(仅3行 inplay), H3 以 开盘→即时 漂移代理',
        'fav_winrate_when_open_underpriced_shortened': s['winrate'],
        'fav_winrate_when_open_overpriced_lengthened': l['winrate'],
        'gap_pp': (s['winrate']-l['winrate'])*100 if s['winrate'] and l['winrate'] else None,
        'interpretation': '开盘线嵌偏差(隐藏战力), 赛前被市场发现而 favorite 变短, 该漂移方向预测赛果 → 赛前是 edge 窗口'
    }

def main():
    print(">>> H1: 1X2 隐藏强队战力 ...")
    h1 = analyze_h1()
    print(f"  总场数={h1['total_matches']:,}")
    print(f"  favorite 实际胜率(全)={h1['fav_actual_winrate_all']:.4f}  开盘隐含={h1['fav_open_implied_all']:.4f}  即时隐含={h1['fav_close_implied_all']:.4f}")
    print(f"  平均 favorite 开盘赔率={h1['avg_open_fav_odds_all']:.3f}")
    print(f"  无脑买 favorite@开盘 ROI={h1['roi_bet_all_favorites_at_open']:.4f}")
    ds=h1['drift_shorter']; dl=h1['drift_longer']
    print(f"  [隐藏战力] favorite 变短(开盘被低估): n={ds['n']:,} 胜率={ds['winrate']:.4f} 均赔={ds['avg_open_fav_odds']:.3f} ROI={ds['roi_bet_fav_at_open_when_shortened']:.4f}")
    print(f"  [对照] favorite 变长(开盘被高估): n={dl['n']:,} 胜率={dl['winrate']:.4f} 均赔={dl['avg_open_fav_odds']:.3f} ROI={dl['roi_bet_fav_at_open_when_lengthened']:.4f}")
    print("  按 favorite 赔率区间:")
    for b,d in h1['by_band'].items():
        def g(x): return f"{x:.3f}" if isinstance(x,float) else str(x)
        print(f"    {b}: n={d['n']:,} 实胜={g(d['fav_actual_winrate'])} 开盘隐含={g(d['fav_open_implied'])} "
              f"全买ROI={g(d['roi_bet_all_fav_at_open'])} | 变短胜={g(d['shorter_winrate'])}/ROI={g(d['shorter_roi'])} "
              f"变长胜={g(d['longer_winrate'])}/ROI={g(d['longer_roi'])}")

    print("\n>>> H2: 半场杀大 / 全场杀小 ...")
    h2 = analyze_h2()
    print(f"  同时有 OU_1H+全场OU 且带半场比分: {h2['matches_with_both_lines']} 场")
    print(f"  全场小球 基准率 = {h2['base_rate_full_under']}")
    print(f"  条件(1H打出大球→全场小球) = {h2['given_1h_over_full_under']}  (n={h2['given_1h_over_n']})")
    print(f"  条件(1H打出大球→全场大球) = {h2['given_1h_over_full_over']}")
    print(f"  lift (条件-基准) = {h2['lift']}")

    print("\n>>> H3: 首个进球后归一化 (开盘→即时 漂移代理) ...")
    h3 = analyze_h3_proxy(h1)
    print(f"  变短胜率={h3['fav_winrate_when_open_underpriced_shortened']:.4f}  变长胜率={h3['fav_winrate_when_open_overpriced_lengthened']:.4f}  gap={h3['gap_pp']:.1f}pp")

    result = {'H1':h1,'H2':h2,'H3_proxy':h3}
    with open("D:/Architecture/analysis/prematch_deviation_result.json","w",encoding="utf-8") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=2)
    print("\n[done] -> analysis/prematch_deviation_result.json")

if __name__=="__main__":
    main()
