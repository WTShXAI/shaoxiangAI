"""半场比分预测回测: 对比 λ 修复前(solve_oip) vs 修复后(OU锚定 implied_total)。

背景: 用户反馈 8-25 之前"中场休息比赛模型能给对比分", 8-30 我做了 λ 反推修复
(势均力敌 solve_oip 压 λ → 改 OU 锚定 implied_total), 但只回测了赛前(minute=0),
**半场/滚球场景没测**。本脚本补这个盲区。

方法: 取每场比赛半场时点(minute 40-50)的滚球 1X2 + OU 赔率 + 当前比分,
分别用两种 λ 逻辑算剩余进球期望 → 终场比分 top1/top3, 对比真实终场比分。
"""
import sqlite3, sys, math, numpy as np
sys.path.insert(0, 'D:/Architecture')
from pipeline.score_model import solve_oip as _solve_oip

DB = 'D:/Architecture/data/events.db'
MAX_GOAL = 8

def solve_oip(ph, pd, pa, max_goal=MAX_GOAL):
    """旧逻辑: solve_oip 反推 λ (8-30 修复前的行为)。"""
    return _solve_oip(ph, pd, pa, max_goal)

def dewater(oh, od, oa):
    inv = 1 / oh + 1 / od + 1 / oa
    return (1 / oh) / inv, (1 / od) / inv, (1 / oa) / inv

def implied_total_from_ou(ou_line, over_w, under_w):
    po = (1 / over_w) / (1 / over_w + 1 / under_w)
    return ou_line + 2.0 * (po - 0.5)

def pois(k, lam):
    if k < 0 or lam <= 0:
        return 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)

def top_scores(lh_rem, la_rem, cur_h, cur_a, max_goal=MAX_GOAL):
    """给定剩余 λ 和当前比分, 算终场比分 top3。"""
    grid = []
    for i in range(max_goal + 1):
        for j in range(max_goal + 1):
            if i < cur_h or j < cur_a:
                continue
            p = pois(i - cur_h, lh_rem) * pois(j - cur_a, la_rem)
            grid.append((f"{i}-{j}", p))
    grid.sort(key=lambda x: -x[1])
    return grid[:3]

def main(n_limit=2000):
    con = sqlite3.connect(DB, timeout=60)
    # 有半场时点 + 有开盘1X2 + 有OU + 终场比分 的比赛
    q = '''
    SELECT DISTINCT m.match_key, m.score_home, m.score_away, m.kickoff
    FROM matches m
    WHERE m.status='finished' AND m.score_home IS NOT NULL
      AND EXISTS (SELECT 1 FROM odds_snapshots s WHERE s.match_key=m.match_key
                  AND s.minute_at>=40 AND s.minute_at<=50
                  AND s.score_at IS NOT NULL AND s.score_at!='')
      AND EXISTS (SELECT 1 FROM odds_snapshots s2 WHERE s2.match_key=m.match_key
                  AND s2.minute_at=0 AND s2.market='1X2' AND s2.odds>1.01)
    ORDER BY m.kickoff DESC LIMIT ?
    '''
    rows = con.execute(q, (n_limit,)).fetchall()

    # 缓存开盘 1X2 和半场时点滚球赔率
    stat = {"A_solve_oip": [0, 0], "B_ou_anchor": [0, 0]}  # [top1命中, top3命中]
    n = 0
    n_skip = 0
    for mk, fh, fa, ko in rows:
        if n >= n_limit:
            break
        # 开盘 1X2
        oh = od = oa = None
        for r in con.execute(
            "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND minute_at=0 AND market='1X2' AND odds>1.01 AND odds<1000 ORDER BY captured_at LIMIT 3", (mk,)):
            if r[0] == 'home': oh = r[1]
            elif r[0] == 'draw': od = r[1]
            elif r[0] == 'away': oa = r[1]
        if not (oh and od and oa):
            n_skip += 1; continue
        # 半场时点: 当前比分 + 滚球 OU(找最接近2.5的线)
        ht = con.execute(
            "SELECT score_at, minute_at, captured_at FROM odds_snapshots WHERE match_key=? AND minute_at>=40 AND minute_at<=50 AND score_at IS NOT NULL AND score_at!='' ORDER BY captured_at DESC LIMIT 1", (mk,)).fetchone()
        if not ht:
            n_skip += 1; continue
        cur_s, minute, cap = ht
        try:
            ch, ca = map(int, str(cur_s).replace(':', '-').split('-')[:2])
        except Exception:
            n_skip += 1; continue
        # 半场时点滚球 OU (取该时刻最接近2.5的 OU 线)
        ou_rows = con.execute(
            "SELECT line, selection, odds FROM odds_snapshots WHERE match_key=? AND market LIKE 'OU_%' AND market NOT LIKE 'OU_1H%' AND market NOT LIKE 'OU_2H%' AND odds>1.01 AND abs(captured_at - ?) < 300 ORDER BY captured_at DESC LIMIT 40", (mk, cap)).fetchall()
        # 聚合每个 line 的 over/under
        lines = {}
        for line_v, selection, odds in ou_rows:
            if line_v is None:
                continue
            try:
                line = float(line_v)
            except Exception:
                continue
            if selection == 'over':
                lines.setdefault(line, {})['over'] = odds
            elif selection == 'under':
                lines.setdefault(line, {})['under'] = odds
        if not lines:
            n_skip += 1; continue
        # 选最接近 2.5 的线
        line = min(lines.keys(), key=lambda x: abs(x - 2.5))
        ou = lines[line]
        if 'over' not in ou or 'under' not in ou:
            n_skip += 1; continue
        over_w, under_w = ou['over'], ou['under']

        # 真实终场比分
        true_s = f"{fh}-{fa}"

        # 剩余时间比例 (半场 45min, 剩 45/90 = 0.5)
        T_ratio = max(0.05, (90.0 - minute) / 90.0)

        # 方案A: solve_oip 反推 λ (旧), 再按剩余时间缩放
        ph, pd_, pa = dewater(oh, od, oa)
        lh_old, la_old = solve_oip(ph, pd_, pa)
        lhA = lh_old * T_ratio
        laA = la_old * T_ratio

        # 方案B: OU 锚定 implied_total (新), 主客按 1X2 比例分配
        it = implied_total_from_ou(line, over_w, under_w)
        ratio = ph / (ph + pa) if (ph + pa) > 0 else 0.5
        lhB = (it * ratio) * T_ratio
        laB = (it * (1 - ratio)) * T_ratio

        for tag, lh, la in (("A_solve_oip", lhA, laA), ("B_ou_anchor", lhB, laB)):
            top = top_scores(lh, la, ch, ca)
            top1 = top[0][0] if top else None
            top3s = [t[0] for t in top]
            if top1 == true_s:
                stat[tag][0] += 1
            if true_s in top3s:
                stat[tag][1] += 1

        n += 1

    print(f"半场比分预测回测 (n={n}, 跳过 {n_skip})")
    print(f"{'方案':16s} {'top1命中':>10s} {'top3命中':>10s}")
    for tag, (t1, t3) in stat.items():
        print(f"{tag:16s} {t1/n*100:9.2f}% {t3/n*100:9.2f}%")

    # 也统计: 方向命中 (比分方向 vs 终场方向)
    return stat, n

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('n', nargs='?', type=int, default=2000)
    args = ap.parse_args()
    main(args.n)
