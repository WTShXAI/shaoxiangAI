# -*- coding: utf-8 -*-
"""
回测: "降0.5盘买小" 经验信号 (OU 总球线漂移检测)
================================================
用户经验: 早盘开大(如 OU 1.5/2.5) 但迟迟不进球, 庄家随后把总球盘口降 0.5,
此时跟"小"反而容易命中。

本脚本:
1. 用 kickoff(GMT+8) + captured_at 重建每场比赛分钟;
2. 逐快照算"隐含总球线" T(t) = over/under 去水概率=0.5 处的盘口 (多线插值);
3. 找"降盘事件": T 从早盘值下降 >= 0.5 且落在时间窗口内;
4. 在触发点买"小"(under) 当前主盘, 结算看最终总球是否 < 主盘线;
5. 报告命中率 / ROI / 对比 naive 基线, 并按时间窗口拆分。

铁律: 数据有据可查, 不伪造; 命中率必须并排 naive 基线; ROI 扣抽水。
"""
import sqlite3, json, math, sys
from collections import defaultdict

GQ = 'D:/Architecture/data/events.db'

def parse_kickoff(s):
    if not s:
        return None
    try:
        from datetime import datetime, timezone, timedelta
        dt = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt.timestamp()
    except Exception:
        return None

def dewatered_over_prob(over_odds, under_odds):
    if not over_odds or not under_odds or over_odds <= 1.01 or under_odds <= 1.01:
        return None
    a, b = 1.0 / over_odds, 1.0 / under_odds
    s = a + b
    if s <= 0:
        return None
    return a / s  # P(goals > L), margin-free

def implied_total_at_snapshot(rows):
    """rows: list of (line:float, over_odds, under_odds). 返回隐含总球线 T (P>L=0.5 处)."""
    pts = []
    for line, o, u in rows:
        p = dewatered_over_prob(o, u)
        if p is None:
            continue
        pts.append((line, p))
    if not pts:
        return None
    pts.sort()
    # 找 P 跨越 0.5 的相邻线, 线性插值
    for i in range(len(pts) - 1):
        l0, p0 = pts[i]
        l1, p1 = pts[i + 1]
        if (p0 - 0.5) * (p1 - 0.5) <= 0 and p0 != p1:
            frac = (0.5 - p0) / (p1 - p0)
            return l0 + frac * (l1 - l0)
    # 不跨越: 全 >0.5 或全 <0.5, 用最近端点外推一点(保守)
    if pts[0][1] > 0.5:
        return pts[0][0] - 0.25  # 极低总球
    return pts[-1][0] + 0.25

def main():
    con = sqlite3.connect(GQ)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 候选比赛: finished + 有 ht/final 比分 + 有足量 OU 快照
    cands = cur.execute("""
        SELECT m.match_key, m.kickoff, m.score_home, m.score_away,
               m.ht_score_home, m.ht_score_away,
               COUNT(*) AS n
        FROM matches m
        JOIN odds_snapshots o ON o.match_key=m.match_key
        WHERE m.status='finished' AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND (m.ht_score_home + m.ht_score_away) < (m.score_home + m.score_away)
          AND o.market LIKE 'OU_%' AND o.market NOT LIKE 'OU_1H%' AND o.market NOT LIKE 'OU_2H%'
        GROUP BY m.match_key
        HAVING n >= 80
        LIMIT 4000
    """).fetchall()
    print(f"候选比赛数: {len(cands)}")

    # 回测参数
    DROP_THRESH = 0.5
    WINDOWS = [(20, 85), (20, 70), (45, 85), (45, 70)]  # (min_start, min_end)
    results = defaultdict(lambda: {'n': 0, 'hit': 0, 'roi_sum': 0.0,
                                   'under_odds_sum': 0.0, 'push': 0})

    # naive 基线: 同主盘线下的历史 under 命中频率
    naive_hit = defaultdict(lambda: [0, 0])  # line_bucket -> [hit, n]

    processed = 0
    for m in cands:
        mk = m['match_key']
        kots = parse_kickoff(m['kickoff'])
        if not kots:
            continue
        total = m['score_home'] + m['score_away']
        # 取该场所有 OU 快照(非半场/全场)并按 captured_at 分组
        snaps = cur.execute("""
            SELECT captured_at,
                   CAST(REPLACE(REPLACE(market,'OU_',''),'_','.') AS REAL) AS line,
                   selection, odds
            FROM odds_snapshots
            WHERE match_key=? AND market LIKE 'OU_%'
              AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%'
        """, (mk,)).fetchall()
        # 按 captured_at 聚合
        by_cap = defaultdict(list)
        for s in snaps:
            cap, line, sel, odds = s['captured_at'], s['line'], s['selection'], s['odds']
            if line is None or odds is None:
                continue
            by_cap[cap].append((line, sel, odds))

        # 逐 captured_at 算 T 与 比赛分钟
        traj = []
        for cap, lst in by_cap.items():
            ov = {}
            for line, sel, odds in lst:
                ov.setdefault(line, {})[sel] = odds
            paired = [(L, d.get('over'), d.get('under')) for L, d in ov.items()
                      if d.get('over') and d.get('under')]
            T = implied_total_at_snapshot(paired)
            if T is None:
                continue
            minute = (cap - kots) / 60.0
            traj.append((minute, T, paired))
        traj.sort()
        if len(traj) < 3:
            continue

        # 早盘 T_open: 取 match_minute 在 [-600, 10] (开赛前后) 的首个有效 T
        open_T = None
        for minute, T, _ in traj:
            if -600 <= minute <= 15:
                open_T = T
                break
        if open_T is None:
            open_T = traj[0][1]

        processed += 1

        # 找降盘事件: 首个 T <= open_T - DROP_THRESH 且落在窗口
        for (w0, w1) in WINDOWS:
            triggered = False
            for minute, T, paired in traj:
                if not (w0 <= minute <= w1):
                    continue
                if T <= open_T - DROP_THRESH:
                    # 触发: 买小当前主盘
                    # 主盘 = 隐含 T 最接近的线
                    main_line = min(paired, key=lambda x: abs(x[0] - T))[0]
                    # under 赔率
                    under_odds = None
                    for L, o, u in paired:
                        if abs(L - main_line) < 1e-6 and u:
                            under_odds = u
                            break
                    if under_odds is None:
                        continue
                    hit = (total < main_line)
                    push = (total == main_line)  # 整数盘口平局(罕见)
                    r = results[(w0, w1)]
                    r['n'] += 1
                    r['under_odds_sum'] += under_odds
                    if hit:
                        r['hit'] += 1
                        r['roi_sum'] += (under_odds - 1)
                    elif push:
                        r['push'] += 1  # 退款, ROI 0
                    else:
                        r['roi_sum'] += -1
                    # naive 基线 (按主盘线分桶)
                    lb = round(main_line * 2) / 2  # 0.5 桶
                    naive_hit[lb][1] += 1
                    if hit:
                        naive_hit[lb][0] += 1
                    triggered = True
                    break  # 每场每窗口只记首个触发
            # 未触发的不计

    print(f"成功处理: {processed} 场\n")
    print("=" * 78)
    print("降0.5盘买小 回测结果 (按时间窗口)")
    print("=" * 78)
    print(f"{'窗口':<12}{'n':>7}{'命中':>8}{'命中率':>9}{'ROI':>9}{'avg小水':>9}{'push':>6}")
    summary = {}
    for (w0, w1) in WINDOWS:
        r = results[(w0, w1)]
        n = r['n']
        if n == 0:
            print(f"{w0}-{w1}'      {0:>7}{'-':>8}{'-':>9}{'-':>9}{'-':>9}{'-':>6}")
            continue
        acc = r['hit'] / n
        roi = r['roi_sum'] / n
        avg_u = r['under_odds_sum'] / n
        print(f"{w0}-{w1}'      {n:>7}{r['hit']:>8}{acc*100:>8.1f}%{roi*100:>+8.1f}%{avg_u:>9.2f}{r['push']:>6}")
        summary[f'{w0}-{w1}'] = {'n': n, 'hit': r['hit'], 'accuracy': round(acc, 4),
                                 'roi': round(roi, 4), 'avg_under_odds': round(avg_u, 3)}

    # naive 基线: 全样本 under 命中频率
    tot_hit = sum(v[0] for v in naive_hit.values())
    tot_n = sum(v[1] for v in naive_hit.values())
    print("\n--- naive 基线 (全样本 under 命中频率, 按主盘线桶) ---")
    for lb in sorted(naive_hit):
        h, n = naive_hit[lb]
        if n >= 10:
            print(f"  主盘线 {lb:>4}: under 命中 {h/n*100:5.1f}%  (n={n})")
    if tot_n:
        print(f"  全样本 under 命中: {tot_hit/tot_n*100:.1f}%  (n={tot_n})")

    # 保存 JSON
    out = {'summary': summary, 'naive_baseline': {str(k): {'hit': v[0], 'n': v[1],
            'acc': round(v[0]/v[1], 4) if v[1] else 0} for k, v in naive_hit.items() if v[1] >= 10},
           'params': {'drop_threshold': DROP_THRESH}}
    with open('analysis/line_drop_backtest.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n已写出 analysis/line_drop_backtest.json")
    con.close()

if __name__ == '__main__':
    main()
