#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
odds_comp_finder.py — 同赔率结构历史场次检索 + 赛果分布 + 各方向ROI
=====================================================================
复用 archive/cleaned_20260804/exploration/find_similar_odds.py::historical_1x2
的 historical_matches 查询作为唯一数据源(SSoT)，在其上叠加：
  - 精确分桶(bucket)：把收盘1X2四舍五入到 tol 网格，同桶=同赔率结构
  - 最近邻TOP-K：欧氏距离最小的历史场
  - 赛果分布：H/D/A 实际频率 vs 庄家去水隐含概率(校准检验)
  - 各方向ROI：每场以"该场自身收盘赔率"下注1单位，平均净收益
  - 可选联赛/日期过滤

历史库 football_data.historical_matches 只有收盘1X2(无AH/OU列)，
故本工具以1X2为赔率结构主体；OU/AH需另接GQ/odds_db(见find_similar_odds)。

用法:
  python odds_comp_finder.py --h 2.10 --d 3.20 --a 3.40
  python odds_comp_finder.py --h 1.85 --d 3.40 --a 4.20 --tol 0.05 --k 50 --league 英超
  python odds_comp_finder.py --h 2.10 --d 3.20 --a 3.40 --html out.html
"""
import sqlite3, argparse, math, html, sys
from collections import Counter

DB = 'D:/Architecture/data/football_data.db'

# 历史行缓存: 按(league,date_from,date_to)键, 批量扫描时只全表扫一次
_ROWS_CACHE = {}


def fetch_rows(league=None, date_from=None, date_to=None):
    key = (league, date_from, date_to)
    if key in _ROWS_CACHE:
        return _ROWS_CACHE[key]
    con = sqlite3.connect(DB); cur = con.cursor()
    q = """SELECT close_home_odds, close_draw_odds, close_away_odds, match_date,
                  home_team, away_team, home_score, away_score, final_result,
                  league_name, total_goals
           FROM historical_matches WHERE close_home_odds > 0"""
    params = []
    if league:
        q += " AND league_name LIKE ?"; params.append(f"%{league}%")
    if date_from:
        q += " AND match_date >= ?"; params.append(date_from)
    if date_to:
        q += " AND match_date <= ?"; params.append(date_to)
    cur.execute(q, params)
    rows = cur.fetchall(); con.close()
    _ROWS_CACHE[key] = rows
    return rows


def bucket_key(h, d, a, tol):
    def r(x):
        return round(round(x / tol) * tol, 2)
    return (r(h), r(d), r(a))


def devig(h, d, a):
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    return (1.0 / h) / inv, (1.0 / d) / inv, (1.0 / a) / inv


def aggregate(subset, th, td, ta):
    n = len(subset)
    if n == 0:
        return None
    cnt = Counter(r[8] for r in subset)  # final_result H/D/A
    H = cnt.get('H', 0); D = cnt.get('D', 0); A = cnt.get('A', 0)

    def roi(side_idx, res_char):
        tot = 0.0
        for r in subset:
            odds = r[side_idx]; fr = r[8]
            tot += (odds - 1) if fr == res_char else -1.0
        return tot / n

    roi_h, roi_d, roi_a = roi(0, 'H'), roi(1, 'D'), roi(2, 'A')
    ph, pd, pa = devig(th, td, ta)
    return dict(n=n, H=H, D=D, A=A, pH=H / n, pD=D / n, pA=A / n,
                roi_h=roi_h, roi_d=roi_d, roi_a=roi_a,
                imp_h=ph, imp_d=pd, imp_a=pa,
                over=(H + A) / n)  # 非平局(分胜负)频率


def analyze(th, td, ta, tol=0.05, k=40, league=None, date_from=None, date_to=None):
    rows = fetch_rows(league, date_from, date_to)
    bt = bucket_key(th, td, ta, tol)
    exact = [r for r in rows if bucket_key(r[0], r[1], r[2], tol) == bt]

    def dist(r):
        return math.hypot(r[0] - th, r[1] - td, r[2] - ta)
    ranked = sorted(rows, key=dist)
    near = ranked[:k]
    return rows, exact, near, dist


def pct(x):
    return f"{x*100:.1f}%"


def build_report(th, td, ta, exact, near, tol, league, out_html=None):
    agg_ex = aggregate(exact, th, td, ta)
    agg_ne = aggregate(near, th, td, ta)
    lines = []
    lines.append(f"目标1X2收盘赔率: 主 {th} / 平 {td} / 客 {ta}  (分桶网格 tol={tol})")
    if league:
        lines.append(f"联赛过滤: {league}")
    lines.append("=" * 72)

    def block(title, agg, subset):
        if agg is None:
            lines.append(f"\n【{title}】 样本=0 → 无历史同结构场次")
            return
        lines.append(f"\n【{title}】 样本 n={agg['n']}")
        lines.append(f"  赛果分布  胜(H) {agg['H']:>4} ({pct(agg['pH'])}) | "
                     f"平(D) {agg['D']:>4} ({pct(agg['pD'])}) | "
                     f"负(A) {agg['A']:>4} ({pct(agg['pA'])}) | 分胜负 {pct(agg['over'])}")
        lines.append(f"  庄家隐含   H {pct(agg['imp_h'])} | D {pct(agg['imp_d'])} | A {pct(agg['imp_a'])}")
        lines.append(f"  ROI(每场以自身收盘赔率下注1单位): "
                     f"押H {agg['roi_h']:+.3f} | 押D {agg['roi_d']:+.3f} | 押A {agg['roi_a']:+.3f}")
        # 校准偏差
        db_h = agg['pH'] - agg['imp_h']
        lines.append(f"  校准偏差(实际-隐含): H {db_h:+.3f}  D {agg['pD']-agg['imp_d']:+.3f}  A {agg['pA']-agg['imp_a']:+.3f}")

    block(f"精确分桶(同赔率结构, tol={tol})", agg_ex, exact)
    block(f"最近邻TOP-{len(near)}", agg_ne, near)

    lines.append("\n" + "=" * 72)
    lines.append(f"最近邻TOP-{min(15, len(near))} 历史场次明细:")
    for r in near[:15]:
        h, d, a, date, home, away, hs, aw, fr, lg, tg = r[:11]
        lines.append(f"  {date} {home} vs {away} [{lg}]  1X2({h:.2f},{d:.2f},{a:.2f})  "
                     f"实际 {hs}-{aw} ({fr})")
    return "\n".join(lines), agg_ex, agg_ne


def to_html(th, td, ta, exact, near, tol, agg_ex, agg_ne, league):
    def row_table(agg, subset):
        if agg is None:
            return "<p style='color:#b00'>样本=0，无历史同结构场次</p>"
        cal = (f"<tr><td>校准偏差(实际-隐含)</td><td>{agg['pH']-agg['imp_h']:+.3f}</td>"
               f"<td>{agg['pD']-agg['imp_d']:+.3f}</td><td>{agg['pA']-agg['imp_a']:+.3f}</td></tr>")
        return f"""
        <table border=1 cellpadding=6 cellspacing=0 style='border-collapse:collapse;font-size:14px'>
          <tr><th>指标</th><th>主胜 H</th><th>平 D</th><th>客胜 A</th></tr>
          <tr><td>样本数 / 频率</td><td colspan=3>n={agg['n']} ｜ 分胜负 {pct(agg['over'])}</td></tr>
          <tr><td>实际赛果频率</td><td>{pct(agg['pH'])}</td><td>{pct(agg['pD'])}</td><td>{pct(agg['pA'])}</td></tr>
          <tr><td>庄家隐含概率</td><td>{pct(agg['imp_h'])}</td><td>{pct(agg['imp_d'])}</td><td>{pct(agg['imp_a'])}</td></tr>
          <tr><td>ROI(每场自身收盘赔率下注1单位)</td><td>{agg['roi_h']:+.3f}</td><td>{agg['roi_d']:+.3f}</td><td>{agg['roi_a']:+.3f}</td></tr>
          {cal}
        </table>"""
    detail = "".join(
        f"<tr><td>{r[3]}</td><td>{html.escape(str(r[4]))} vs {html.escape(str(r[5]))}</td>"
        f"<td>{html.escape(str(r[9]))}</td><td>{r[0]:.2f},{r[1]:.2f},{r[2]:.2f}</td>"
        f"<td>{r[6]}-{r[7]} ({r[8]})</td></tr>" for r in near[:20])
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>同赔率结构历史场次分析</title></head><body style='font-family:Segoe UI,Microsoft YaHei;background:#0f1115;color:#e6e6e6;padding:24px'>
<h2>同赔率结构历史场次分析</h2>
<p>目标1X2收盘赔率: <b>主 {th} / 平 {td} / 客 {ta}</b> ｜ 分桶网格 tol={tol}{' ｜ 联赛: '+league if league else ''}</p>
<h3>① 精确分桶（同赔率结构）</h3>{row_table(agg_ex, exact)}
<h3>② 最近邻 TOP-{len(near)}</h3>{row_table(agg_ne, near)}
<h3>③ 最近邻明细 (TOP 20)</h3>
<table border=1 cellpadding=5 cellspacing=0 style='border-collapse:collapse;font-size:13px'>
<tr><th>日期</th><th>对阵</th><th>联赛</th><th>收盘1X2</th><th>实际比分(赛果)</th></tr>{detail}</table>
<p style='color:#888;font-size:12px'>数据源: football_data.historical_matches (31.2万场, 收盘1X2+赛果)。ROI为描述性历史统计, 非未来收益保证。</p>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--h', type=float, required=True)
    ap.add_argument('--d', type=float, required=True)
    ap.add_argument('--a', type=float, required=True)
    ap.add_argument('--tol', type=float, default=0.05)
    ap.add_argument('--k', type=int, default=40)
    ap.add_argument('--league', default=None)
    ap.add_argument('--date-from', default=None)
    ap.add_argument('--date-to', default=None)
    ap.add_argument('--html', default=None)
    args = ap.parse_args()

    rows, exact, near, _ = analyze(args.h, args.d, args.a, args.tol, args.k,
                                   args.league, args.date_from, args.date_to)
    txt, agg_ex, agg_ne = build_report(args.h, args.d, args.a, exact, near,
                                       args.tol, args.league, args.html)
    print(txt)
    print(f"\n[总历史池] {len(rows)} 场 (已按过滤条件)")
    if args.html:
        html_text = to_html(args.h, args.d, args.a, exact, near, args.tol,
                            agg_ex, agg_ne, args.league)
        with open(args.html, 'w', encoding='utf-8') as f:
            f.write(html_text)
        print(f"[已写出HTML] {args.html}")


if __name__ == '__main__':
    main()
