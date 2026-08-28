"""
OU 不对称诱导校准扫描器 v2 (2026-08-18 修正版)
===============================================
v1 的两个 bug 修正:
  ① 细价位桶(±0.015) → 过拟合噪声, 相邻值 edge 跳变 25pp 不合理; 改宽分段。
  ② 0.25 步长线(2.25/2.75) half-win 结算错误(把 3 球直接 skip, 系统性拉低 over/under 命中率);
     正确结算: over 期望 = P(>line) + 0.5*P(==line+0.25), under 期望 = P(<line) + 0.5*P(==line-0.25)。

分段(按业务逻辑, 非机械等宽): 1.40 / 1.60 / 1.75 / 1.90 / 2.00 / 2.15 / 2.30 / +inf
输出双向校准表: edge>=+5pp 且 n>=300 = 甜点(该方向概率 +edge); edge<=-5pp 且 n>=300 = 陷阱(该方向概率 +edge, 即降权)。
单庄(乐鱼)数据; 赛前快照验证。

输出: deliverables/ou_inducement_calibration.json (v2 覆盖)
"""
import os, sys, sqlite3, json
import numpy as np

sys.path.insert(0, 'D:/Architecture')
GQ = 'D:/Architecture/data/events.db'
OUT = 'D:/Architecture/deliverables/ou_inducement_calibration.json'

MIN_ABS_EDGE = 0.05   # |edge|>=5pp
MIN_N = 300           # 分段样本下限
LINES = [(1.5, 0.5), (2.0, 0.5), (2.5, 0.5), (3.0, 0.5), (2.25, 0.25), (2.75, 0.25)]
BREAKS = [1.40, 1.60, 1.75, 1.90, 2.00, 2.15, 2.30, 99.0]


def devig(o, u):
    try:
        o, u = float(o), float(u)
        if o <= 0 or u <= 0:
            return None
        return 1.0 / o / (1.0 / o + 1.0 / u)
    except Exception:
        return None


def settle(t, line, step, side):
    """正确结算: 返回该方向的期望命中(0/0.5/1)。"""
    if step == 0.5:
        if side == 'over':
            return 1.0 if t > line else 0.0
        return 1.0 if t < line else 0.0
    else:  # 0.25 步长
        if side == 'over':
            if t > line:
                return 1.0
            if t == line + 0.25:
                return 0.5
            return 0.0
        else:
            if t < line:
                return 1.0
            if t == line - 0.25:
                return 0.5
            return 0.0


def scan_line(cur, line, step):
    mkt = f'OU_{line:.2f}'
    rows = cur.execute('''
      SELECT m.score_home, m.score_away, s.over_odds, s.under_odds
      FROM matches m JOIN (
        SELECT match_key, MAX(CASE WHEN selection='over' THEN odds END) AS over_odds,
               MAX(CASE WHEN selection='under' THEN odds END) AS under_odds
        FROM odds_snapshots WHERE market=? AND odds>1.01 AND odds<=1000 GROUP BY match_key
      ) s ON s.match_key=m.match_key
      WHERE m.status='finished' AND m.score_home IS NOT NULL
        AND s.over_odds IS NOT NULL AND s.under_odds IS NOT NULL
    ''', (mkt,)).fetchall()
    out = []
    for side, idx in (('over', 2), ('under', 3)):
        for i in range(len(BREAKS) - 1):
            lo, hi = BREAKS[i], BREAKS[i + 1]
            sub = [r for r in rows if lo <= float(r[idx]) < hi]
            if len(sub) < MIN_N:
                continue
            exp = sum(settle(int(r[0]) + int(r[1]), line, step, side) for r in sub) / len(sub)
            imps = [devig(r[2], r[3]) for r in sub]
            imps = [x for x in imps if x is not None]
            imp = np.mean(imps) if imps else 0.5
            if side == 'under':
                imp = 1.0 - imp
            edge = exp - imp
            if abs(edge) >= MIN_ABS_EDGE:
                out.append({
                    'line': line, 'side': side,
                    'odds_lo': lo, 'odds_hi': hi,
                    'n': len(sub),
                    'actual': round(float(exp), 4),
                    'implied': round(float(imp), 4),
                    'edge': round(float(edge), 4),
                    'calib_pp': round(float(edge) * 100, 1),
                })
    return out, len(rows)


def main():
    con = sqlite3.connect(f'file:{GQ}?mode=ro', uri=True, timeout=30)
    cur = con.cursor()
    all_rows = []
    line_stats = []
    for line, step in LINES:
        segs, n = scan_line(cur, line, step)
        all_rows.extend(segs)
        line_stats.append({'line': line, 'n': n, 'segs': len(segs)})
        print(f'OU_{line:g}({step:g}步): {n} 场, {len(segs)} 个有效区段')

    # 按 edge 绝对值排序
    all_rows.sort(key=lambda x: -abs(x['edge']))
    sweet = [s for s in all_rows if s['edge'] > 0]
    trap = [s for s in all_rows if s['edge'] < 0]

    print(f'\n=== 甜点(正向 edge, 该方向概率上调): {len(sweet)} 段 ===')
    for s in sweet:
        print(f"  OU_{s['line']:g} {s['side']:5s} @[{s['odds_lo']:.2f},{s['odds_hi']:.2f}): n={s['n']:4d} 实际{s['actual']*100:.1f}% 隐含{s['implied']*100:.1f}% edge=+{s['edge']*100:.1f}pp")
    print(f'\n=== 陷阱(负向 edge, 该方向概率下调): {len(trap)} 段 ===')
    for s in trap:
        print(f"  OU_{s['line']:g} {s['side']:5s} @[{s['odds_lo']:.2f},{s['odds_hi']:.2f}): n={s['n']:4d} 实际{s['actual']*100:.1f}% 隐含{s['implied']*100:.1f}% edge={s['edge']*100:.1f}pp")

    out = {
        'version': 2,
        'generated_at': '2026-08-18',
        'source': 'events.db odds_snapshots 赛前 + matches finished',
        'method': '宽分段(|edge|>=5pp 且 n>=300) + 正确half-win结算; edge=实际命中率-去水隐含',
        'note': 'v2 修正: ①细桶过拟合改宽分段; ②0.25步长线half-win结算错误修正。v1细桶42条甜点作废。',
        'line_stats': line_stats,
        'sweet': sweet,
        'trap': trap,
        'usage': 'ou_inducement_calibrator 加载; 命中段 → 该方向概率 +edge(甜点) / +edge(陷阱,即降权)',
        'caveats': [
            '单庄(乐鱼)数据, edge=该庄特定价位系统性误定价, 非市场共识',
            '赛前快照验证, 滚球中态需新数据二次验证',
        ],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f'\n[save] {OUT} (甜点{len(sweet)} + 陷阱{len(trap)})')
    con.close()


if __name__ == '__main__':
    main()
