"""
Live Goal Probe — 规则模型回测 (Task #91)

方法:
- 取 events.db 中已结束且含半场比分 + 全场比分的比赛。
- 用每场 EARLIEST 盘口快照(≈开赛/早盘, minute_at 最小) 构建 odds 字典,
  喂给与线上完全一致的 probe_core() (无动量项, 因为开赛瞬间无 2 分钟漂移)。
- 假定开赛比分 0-0, minute=0 (与线上"新比赛默认 0-0"一致)。
- 对比模型给出的 半场/全场 方向 与 真实结果。

诚实性:
- 半场方向 = 所选 OU_1H line 的 over/under; 全场方向 = 所选 OU line 的 over/under。
- 校准: 把模型概率分桶, 看真实命中率是否贴近预测(贴近=诚实概率仪; 不贴近=虚高)。
- 边缘: 若绿灯方向能稳定跑赢赔率(ROI>0), 才有"真edge"; 否则只是跟随市场。
"""
import os, sqlite3, json
from collections import defaultdict
from analysis.live_goal_probe import probe_core, load_calibration

GQ = os.environ.get('GQ_DB', 'D:/Architecture/data/events.db')


def earliest_odds(con, match_key):
    """取每场最早(开赛)快照的 over/under (OU_1H_*/OU_*) 与 1X2 odds。"""
    cur = con.cursor()
    rows = cur.execute(
        """SELECT market, selection, odds, captured_at FROM odds_snapshots
           WHERE match_key=? AND selection IN ('over','under','home','draw','away')
             AND odds>? AND odds<?
           ORDER BY captured_at ASC""",
        (match_key, 1.01, 1000.0)).fetchall()
    by_mkt = {}
    for mkt, sel, odds, cap in rows:
        by_mkt.setdefault(mkt, {}).setdefault(cap, {})[sel] = odds
    out = {}
    for mkt, caps in by_mkt.items():
        for cap in sorted(caps):
            d = caps[cap]
            if mkt.startswith('OU'):
                if 'over' in d and 'under' in d:
                    out[f'{mkt}__over'] = d['over']
                    out[f'{mkt}__under'] = d['under']
                    break
            else:  # 1X2
                for s in ('home', 'draw', 'away'):
                    if s in d:
                        out[f'1X2__{s}'] = d[s]
                break
    return out


def implied_over_prob(over_odds, under_odds, margin=0.0):
    p_o = 1.0 / over_odds
    p_u = 1.0 / under_odds
    s = p_o + p_u
    if s <= 0:
        return None
    return (p_o / s) * (1 - margin)


def main(limit=20000):
    con = sqlite3.connect(GQ)
    cur = con.cursor()
    matches = cur.execute(
        """SELECT match_key, league, ht_score_home, ht_score_away, score_home, score_away
           FROM matches
           WHERE status='finished'
             AND ht_score_home IS NOT NULL AND ht_score_away IS NOT NULL
             AND score_home IS NOT NULL AND score_away IS NOT NULL
             AND (ht_score_home + ht_score_away) < (score_home + score_away)"""
    ).fetchall()

    recs = []
    for mk, league, h1, a1, hf, af in matches:
        odds = earliest_odds(con, mk)
        # 需要至少 OU_1H 或 OU 才有意义
        has_ou1h = any(k.startswith('OU_1H_') and k.endswith('__over') for k in odds)
        has_ou = any(k.startswith('OU_') and not k.startswith('OU_1H') and not k.startswith('OU_2H') and k.endswith('__over') for k in odds)
        if not (has_ou1h or has_ou):
            continue
        res = probe_core(odds, '0-0', 0, league, con=None)
        ht_goals = h1 + a1
        ft_goals = hf + af
        recs.append({
            'mk': mk, 'league': league,
            'ht_goals': ht_goals, 'ft_goals': ft_goals,
            'half': res['half'], 'full': res['full'],
        })

    print(f"回测样本(含 OU_1H 或 OU 盘口的已结束比赛): {len(recs)}")

    def analyze(field, goals_key, label):
        bettable = [r for r in recs if r[field]['direction'] is not None
                    and r[field]['over_odds'] is not None]
        n_bet = len(bettable)
        correct = sum(1 for r in bettable
                      if (r[field]['direction'] == 'OVER') == (r[goals_key] > r[field]['line']))
        acc = correct / n_bet if n_bet else 0.0

        # 按信号拆分
        by_sig = defaultdict(lambda: [0, 0])
        for r in bettable:
            d = r[field]
            win = (d['direction'] == 'OVER') == (r[goals_key] > d['line'])
            by_sig[d['signal']][0] += int(win)
            by_sig[d['signal']][1] += 1

        # 校准: 模型概率分桶 vs 真实"破该 line 的 over"命中率
        cali = defaultdict(lambda: [0, 0])
        for r in recs:
            d = r[field]
            if d['over_odds'] is None:
                continue
            over_win = r[goals_key] > d['line']
            b = round(d['prob'] * 10) / 10
            cali[b][0] += int(over_win)
            cali[b][1] += 1

        # ROI: 绿灯方向按盘口赔率下注
        roi_sum = 0.0
        for r in bettable:
            d = r[field]
            o = d['over_odds'] if d['direction'] == 'OVER' else d['under_odds']
            win = (d['direction'] == 'OVER') == (r[goals_key] > d['line'])
            roi_sum += (o - 1) if win else -1
        roi = roi_sum / n_bet if n_bet else 0.0

        # 市场隐含概率校准(参照)
        mkt_cali = defaultdict(lambda: [0, 0])
        for r in recs:
            d = r[field]
            if d['over_odds'] is None or d['under_odds'] is None:
                continue
            imp = implied_over_prob(d['over_odds'], d['under_odds'])
            if imp is None:
                continue
            over_win = r[goals_key] > d['line']
            b = round(imp * 10) / 10
            mkt_cali[b][0] += int(over_win)
            mkt_cali[b][1] += 1

        print(f"\n=== {label} ===")
        print(f"可下注样本(有方向且有赔率): {n_bet}")
        print(f"方向命中率(绿灯方向 vs 真实): {acc*100:.1f}%  ({correct}/{n_bet})")
        print(f"盲跟市场低水方 ROI: {roi*100:+.1f}%  (盈亏平衡需 > -抽水≈-11%)")
        print("按信号拆分命中率:")
        for sig in ['STRONG_BREAK', 'STRONG_HOLD', 'WEAK_TREND']:
            if sig in by_sig:
                w, n = by_sig[sig]
                print(f"  {sig:14s}: {w/n*100:5.1f}%  ({w}/{n})")
        print("模型概率 → 真实 over(line) 命中率(校准):")
        for b in sorted(cali):
            w, n = cali[b]
            if n >= 5:
                print(f"  prob {b:.1f}: 实际 {w/n*100:5.1f}%  (n={n})")
        print("市场隐含概率 → 真实 over(line) 命中率(参照, 应贴近):")
        for b in sorted(mkt_cali):
            w, n = mkt_cali[b]
            if n >= 5:
                print(f"  imp {b:.1f}: 实际 {w/n*100:5.1f}%  (n={n})")

        return {
            'label': label,
            'n_bettable': n_bet,
            'direction_accuracy': round(acc, 4),
            'roi': round(roi, 4),
            'by_signal': {sig: {'hit': by_sig[sig][0], 'n': by_sig[sig][1],
                               'acc': round(by_sig[sig][0]/by_sig[sig][1], 4) if by_sig[sig][1] else None}
                         for sig in ['STRONG_BREAK', 'STRONG_HOLD', 'WEAK_TREND'] if sig in by_sig},
            'model_calibration': {str(b): {'hit': cali[b][0], 'n': cali[b][1],
                                          'rate': round(cali[b][0]/cali[b][1], 4)} for b in sorted(cali) if cali[b][1] >= 5},
            'market_calibration': {str(b): {'hit': mkt_cali[b][0], 'n': mkt_cali[b][1],
                                            'rate': round(mkt_cali[b][0]/mkt_cali[b][1], 4)} for b in sorted(mkt_cali) if mkt_cali[b][1] >= 5},
        }

    summary = {
        'generated_at': __import__('datetime').datetime.now().isoformat(timespec='seconds'),
        'method': '开赛快照(最早 minute_at 盘口) 喂入与线上一致的 probe_core(); 假定 0-0/0 分钟; 无动量项',
        'n_matches_with_odds': len(recs),
        'half': analyze('half', 'ht_goals', '半场破蛋 (所选 OU_1H line 的 over/under)'),
        'full': analyze('full', 'ft_goals', '全场破蛋 (所选 OU line 的 over/under)'),
    }
    # 半场 ≥1 球校准
    cali1 = defaultdict(lambda: [0, 0])
    for r in recs:
        d = r['half']
        if d['over_odds'] is None:
            continue
        ge1 = r['ht_goals'] >= 1
        b = round(d['prob'] * 10) / 10
        cali1[b][0] += int(ge1)
        cali1[b][1] += 1
    summary['half_ge1_calibration'] = {str(b): {'hit': cali1[b][0], 'n': cali1[b][1],
                                                'rate': round(cali1[b][0]/cali1[b][1], 4)} for b in sorted(cali1) if cali1[b][1] >= 5}

    out_path = os.path.join(os.path.dirname(__file__), 'live_goal_probe_backtest.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n已写出回测摘要: {out_path}")
    return summary

    con.close()
    return recs


if __name__ == '__main__':
    main()
