#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""特征回测 (2026-09-10, 用户: 用特征回测一下).

问题: 哪些特征真正提升 中场(HT) OU 方向 / 赛前 OU 方向 的判定准确率?

特征 (HT 时点仅上半场信息):
  F1 池隐含   unified_scoreline@HT 的 top5 隐含 P(FT总球 > 线)
  F2 市场OU   HT 窗口最新 OU 去水 P(over)
  F3 时段     周三≥18点 / 周六 (用户手册规律)
  F4 已进总球 HT 总球 T (相对线: T - line)

目标: FT总球 vs 线 (开盘线) 的 大/小。
方法: 单特征准确率(0.5阈值) + 贪心组合 + 简单逻辑回归(梯度下降, 纯python)。
用法: python scripts/backtest_features_ht.py [--days 30] [--limit 300]
"""
import argparse
import collections
import datetime
import math
import sys
import warnings

sys.path.insert(0, r'D:\Architecture')
warnings.filterwarnings('ignore')
from analysis.live_goal_probe import _open_gq, _dewater_1x2, _open_1x2_from_snapshots, _open_total_from_snapshots  # noqa
from pipeline.cs_db_match import unified_scoreline  # noqa


def parse_ko(s):
    try:
        dt = datetime.datetime.fromisoformat(str(s).replace(' ', 'T'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
        return dt
    except Exception:
        return None


def in_slot(dt):
    return (dt.weekday() == 2 and dt.hour >= 18) or (dt.weekday() == 5)


def sigmoid(z):
    return 1 / (1 + math.exp(-max(-30, min(30, z))))


def logistic_fit(xs, ys, iters=300, lr=0.1):
    w = [0.0] * len(xs[0])
    b = 0.0
    n = len(xs)
    for _ in range(iters):
        gw = [0.0] * len(w)
        gb = 0.0
        for x, y in zip(xs, ys):
            p = sigmoid(sum(wi * xi for wi, xi in zip(w, x)) + b)
            e = p - y
            for i in range(len(w)):
                gw[i] += e * x[i]
            gb += e
        w = [wi - lr * g / n for wi, g in zip(w, gw)]
        b -= lr * gb / n
    return w, b


def main(days=30, limit=300):
    con = _open_gq()
    # 2026-09-10 性能: 不做 68M 大表 JOIN — 先选完赛场(小表), 再按 match_key 索引逐场查 HT 帧
    mrows = con.execute("""
        SELECT match_key, kickoff, score_home, score_away FROM matches
        WHERE status='finished' AND score_home IS NOT NULL AND kickoff >= datetime('now', ?)
        ORDER BY kickoff DESC LIMIT ?""", (f'-{days} day', limit)).fetchall()
    best = {}
    for mk, ko, fsh, fsa in mrows:
        sc_row = con.execute("""
            SELECT score_at, minute_at FROM odds_snapshots
            WHERE match_key=? AND score_at IS NOT NULL AND score_at != ''
              AND minute_at BETWEEN 40 AND 50
            ORDER BY ABS(minute_at - 45) LIMIT 1""", (mk,)).fetchone()
        if sc_row:
            best[mk] = (ko, fsh, fsa, sc_row[0])
    print(f'HT 可回放场: {len(best)}')

    data = []
    for mk, (ko, fsh, fsa, ht_sc) in best.items():
        try:
            dt = parse_ko(ko)
            if not dt:
                continue
            h, d, a = _open_1x2_from_snapshots(con, mk)
            if not (h and d and a):
                continue
            ou_line, _T = _open_total_from_snapshots(con, mk, 'OU_', exclude_prefixes=['OU_1H', 'OU_2H'], ref_line=2.5)
            if not ou_line:
                continue
            sh, sa = (int(x) for x in ht_sc.split('-'))
            total_now = sh + sa
            # F1 池隐含 (HT 态)
            dm = unified_scoreline(h=h, d=d, a=a, ou_line=ou_line, current_score=ht_sc, current_minute=45)
            t5 = (dm or {}).get('top5') or []
            if len(t5) < 3:
                continue
            tot5 = sum(t['prob'] for t in t5) or 1.0
            p_pool = sum(t['prob'] / tot5 for t in t5
                         if sum(int(x) for x in t['score'].split('-')) > ou_line)
            # F2 市场 OU: HT 窗口内最新 over/under (取窗口中位线: 最接近 ou_line 的 HT 帧)
            kots = dt.timestamp()
            fr = con.execute("""
                SELECT market, odds FROM odds_snapshots
                WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%'
                  AND market NOT LIKE 'OU_2H%' AND odds > 1.01 AND odds < 30
                  AND captured_at BETWEEN ? AND ?
                ORDER BY captured_at DESC LIMIT 60""", (mk, kots + 38 * 60, kots + 52 * 60)).fetchall()
            pairs = {}
            for mkt, od in fr:
                pairs.setdefault(mkt, []).append(od)
            p_mkt = None
            for mkt, ods in pairs.items():
                if len(ods) >= 2:
                    inv = [1 / ods[0], 1 / ods[1]]
                    pp = inv[0] / (inv[0] + inv[1])   # ods[0]=over, ods[1]=under (捕获序)
                    p_mkt = pp
                    break
            if p_mkt is None:
                continue
            f3 = 1.0 if in_slot(dt) else 0.0
            f4 = total_now - ou_line
            y = 1 if (fsh + fsa) > ou_line else 0
            data.append({'mk': mk, 'y': y, 'f1': p_pool, 'f2': p_mkt, 'f3': f3, 'f4': f4 / 3.0})
        except Exception:
            continue
    print(f'有效样本 n={len(data)}')

    def acc(pred):
        c = collections.Counter()
        for r in data:
            c[(r['y'] == (1 if pred(r) >= 0.5 else 0))] += 1
        return f"{c[True]/max(1,sum(c.values()))*100:.1f}%"

    base = sum(r['y'] for r in data) / max(1, len(data))
    print(f'\n基准(大球占比): {base*100:.1f}%')
    print(f'F1 池隐含:      {acc(lambda r: r["f1"])}')
    print(f'F2 市场OU:      {acc(lambda r: r["f2"])}')
    print(f'F4 已进总球差:  {acc(lambda r: 0.5 + r["f4"] * 0.2)}')
    print(f'F1+F2 均值:     {acc(lambda r: (r["f1"] + r["f2"]) / 2)}')
    print(f'F3 时段 单独:   {acc(lambda r: 0.25 + r["f3"] * 0.5)}  (基准偏置 {max(base, 1-base)*100:.1f}%)')
    # 逻辑回归组合
    xs = [[r['f1'], r['f2'], r['f4']] for r in data]
    ys = [r['y'] for r in data]
    w, b = logistic_fit(xs, ys)
    c = collections.Counter()
    for x, y in zip(xs, ys):
        c[(sigmoid(sum(wi * xi for wi, xi in zip(w, x)) + b) >= 0.5) == (y == 1)] += 1
    print(f'LogReg(F1,F2,F4): {c[True]/len(data)*100:.1f}%  w={[round(x, 2) for x in w]} b={round(b, 2)}')
    # 时段增量: 在 LogReg 上加 F3
    xs3 = [[r['f1'], r['f2'], r['f4'], r['f3']] for r in data]
    w, b = logistic_fit(xs3, ys)
    c = collections.Counter()
    for x, y in zip(xs3, ys):
        c[(sigmoid(sum(wi * xi for wi, xi in zip(w, x)) + b) >= 0.5) == (y == 1)] += 1
    print(f'LogReg(+F3时段): {c[True]/len(data)*100:.1f}%  w={[round(x, 2) for x in w]} (F3权重显著>0才值得进特征)')
    con.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=30)
    ap.add_argument('--limit', type=int, default=300)
    a = ap.parse_args()
    main(a.days, a.limit)
