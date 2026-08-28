"""GQ 实时 OU 盘口 vs 真实赛果 的 edge/校准探测.

核心认知: 赛果(总进球)就是 OU 结算面.
GQ.match_outcomes 1844 场全部已完场(均有 score), 其中 1180 场带 op_ou_line/over/under.
对每场:
  - 乐鱼去水隐含 P(over) = (1/over) / (1/over + 1/under)   [单庄去水]
  - 实际大球 = (score_home+score_away) > op_ou_line
聚合两层:
  L1 全局校准: 按盘口线区间分桶, 看乐鱼隐含 P(over) vs 实际大球率 的系统偏差.
  L2 联赛级偏差: GQ 内部同联赛聚合, 找"乐鱼隐含 vs 该联赛真实大球率"偏离最大的联赛.

诚实边界(铁律): 这是单庄(乐鱼)盘口去水隐含概率 vs 赛果频率的校准偏差/陷阱信号,
并非跨庄套利 edge. GQ 联赛多为 obscure/杯赛, 与我们有13赛季赛果历史的 mainstream 联赛
命名不重叠, 故赛果侧"真实概率"用 GQ 自身同联赛赛果(自洽), 样本小者标注不显著.

输出: data/gq_ou_edge_probe.csv + 控制台摘要.
"""
import sqlite3, csv, collections, statistics

GQ = "data/events.db"
OUT = "data/gq_ou_edge_probe.csv"
HIST = "data/football_data.db"

gq = sqlite3.connect(GQ)
cur = gq.cursor()
cur.execute("""
SELECT league, op_ou_line, op_ou_over, op_ou_under, score_home, score_away
FROM match_outcomes
WHERE op_ou_line IS NOT NULL AND op_ou_over>0 AND op_ou_under>0
  AND score_home IS NOT NULL AND score_away IS NOT NULL
""")
rows = cur.fetchall()
gq.close()

# 联赛精确命中 historical?
hist = sqlite3.connect(HIST)
hcur = hist.cursor()
hcur.execute("SELECT DISTINCT league_name FROM historical_matches")
hist_leagues = set(r[0] for r in hcur.fetchall())
hist.close()
gq_leagues = set(r[0] for r in rows)
exact_hit = [lg for lg in gq_leagues if lg in hist_leagues]

def implied_p_over(ov, un):
    io, iu = 1.0/ov, 1.0/un
    s = io + iu
    return io/s if s > 0 else None

recs = []
for lg, line, ov, un, sh, sa in rows:
    p = implied_p_over(ov, un)
    if p is None:
        continue
    actual = 1 if (sh + sa) > line else 0
    recs.append(dict(league=lg, line=line, implied=p, actual=actual,
                     margin=(1.0/ov + 1.0/un) - 1))

n_total = len(recs)
over_all = statistics.mean(r['actual'] for r in recs)
imp_all = statistics.mean(r['implied'] for r in recs)

print(f"GQ OU 有效场次 = {n_total} (全部已完场)")
print(f"GQ有OU联赛数 = {len(gq_leagues)}  精确命中 historical 历史联赛 = {len(exact_hit)} {exact_hit[:10]}")
print(f"整体: 乐鱼隐含P(over)={imp_all*100:.1f}%  实际大球率={over_all*100:.1f}%  "
      f"全局偏差={(imp_all-over_all)*100:+.1f}pp")
print(f"整体去水margin均值 = {statistics.mean(r['margin'] for r in recs)*100:.1f}%\n")

def bucket(line):
    if line < 1.5: return "<1.5"
    if line < 2.5: return "1.5-2.5"
    if line < 3.5: return "2.5-3.5"
    return ">=3.5"

bk = collections.defaultdict(list)
for r in recs:
    bk[bucket(r['line'])].append(r)

print("=== L1 全局校准 (按盘口线区间) ===")
print(f"{'区间':<10}{'n':>5}{'implied':>9}{'actual':>8}{'bias':>7}")
order = ["<1.5", "1.5-2.5", "2.5-3.5", ">=3.5"]
for b in order:
    rs = bk.get(b, [])
    if not rs:
        continue
    imp = statistics.mean(x['implied'] for x in rs)
    act = statistics.mean(x['actual'] for x in rs)
    print(f"{b:<10}{len(rs):>5}{imp*100:>8.1f}%{act*100:>7.1f}%{(imp-act)*100:>6.1f}pp")

# L2 联赛级
by_lg = collections.defaultdict(list)
for r in recs:
    by_lg[r['league']].append(r)
lg_stats = []
for lg, rs in by_lg.items():
    imp = statistics.mean(x['implied'] for x in rs)
    act = statistics.mean(x['actual'] for x in rs)
    lg_stats.append((lg, len(rs), statistics.mean(x['line'] for x in rs), imp, act, imp-act))
lg_stats.sort(key=lambda x: -abs(x[5]))

print(f"\n=== L2 联赛级偏差 (|隐含-实际|排序, n=联赛内样本) ===")
print(f"{'联赛':<18}{'n':>4}{'line':>6}{'implied':>9}{'actual':>8}{'bias':>8}  方向")
for lg, nn, line, imp, act, bias in lg_stats[:12]:
    d = "大球被高估→买小" if bias > 0 else "大球被低估→买大"
    flag = " ⚠小样本" if nn < 20 else ""
    print(f"{lg:<18}{nn:>4}{line:>6.2f}{imp*100:>8.1f}%{act*100:>7.1f}%{bias*100:>7.1f}pp  {d}{flag}")
if len(lg_stats) > 22:
    print("  ... (中间省略) ...")
    for lg, nn, line, imp, act, bias in lg_stats[-8:]:
        d = "大球被高估→买小" if bias > 0 else "大球被低估→买大"
        flag = " ⚠小样本" if nn < 20 else ""
        print(f"{lg:<18}{nn:>4}{line:>6.2f}{imp*100:>8.1f}%{act*100:>7.1f}%{bias*100:>7.1f}pp  {d}{flag}")

with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["league", "n", "avg_line", "implied_p_over", "actual_over_rate",
                "bias_pp", "direction", "small_sample"])
    for lg, nn, line, imp, act, bias in lg_stats:
        direction = "大球被高估(买小值博)" if bias > 0 else "大球被低估(买大值博)"
        w.writerow([lg, nn, round(line, 2), round(imp, 4), round(act, 4),
                    round(bias*100, 2), direction, "yes" if nn < 20 else "no"])
print(f"\nCSV -> {OUT}")
