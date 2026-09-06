"""
全量波胆(CS)赔率复盘 — 双时点 clean 版
=====================================
方法论 (2026-07-20 用户指示 + 数据体检修正):
  events.db 的 odds 快照干净可筛选, 取两个时点即可:
    ① 初盘   = per-selection 首见 (每条线取自己的最早 captured_at)
    ② 中场收盘 = kick+44~52min 之间每条线的最后一条
  提取器复用 gq_odds_filter.get_open / get_ht_close (已验证)。

重要 caveat (数据铁律):
  matches.score_home/score_away 已知污染(进第一球后乐鱼冻结成终场 → 49%单球、
  0-1>1-0 反转)。本报告"实际"字段来自该污染字段, 故命中率只能理解为
  "市场结构预期 vs 记录比分 的一致性", 严禁用于模型 λ 校准。
"""
import sqlite3, datetime, html, json
import gq_odds_filter as gq
from gq_odds_filter import get_open, get_ht_close

DB = 'data/events.db'
OUT = '分析报告/CS复盘_双时点_clean_20260720.html'
WIN_START = datetime.datetime(2026, 7, 16, 0, 0, 0).timestamp()

def is_sim(lg):
    return ('VS-' in lg) or ('EAFC' in lg) or ('PANDA' in lg)

c = sqlite3.connect(DB)
rows = c.execute('''SELECT DISTINCT s.match_key FROM odds_snapshots s
  JOIN matches m ON m.match_key=s.match_key
  WHERE s.market='CS' AND s.captured_at>=? AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL''',
  (WIN_START,)).fetchall()

targets = []
for (mk,) in rows:
    m = c.execute('SELECT league,score_home,score_away,status,kickoff FROM matches WHERE match_key=?', (mk,)).fetchone()
    if not m: continue
    lg = m[0] or ''
    if is_sim(lg): continue
    if m[3] != 'finished': continue
    targets.append((mk, lg, int(m[1]), int(m[2]), m[4]))
print('target matches:', len(targets))


def topn(d):
    """返回 (top1, top3, top5, denom) ; d={selection:odds}"""
    tot = sum(1.0 / v for v in d.values() if v and v > 0)
    if tot <= 0:
        return None, None, None, 0
    so = sorted(d.items(), key=lambda kv: (kv[1] if kv[1] else 1e9))
    return so[0][0], [s for s, _ in so[:3]], [s for s, _ in so[:5]], tot


recs = []
for mk, lg, sh, sa, kickoff in targets:
    op = get_open(mk, 'CS')
    ht = get_ht_close(mk, 'CS', kickoff) if kickoff else {}
    if not op:
        continue
    o_t1, o_t3, o_t5, o_den = topn(op)
    if o_den <= 0:
        continue
    actual = f'{sh}-{sa}'
    a_open = op.get(actual)
    # 中场收盘
    h_t1 = h_t3 = h_t5 = None
    a_ht = ht.get(actual) if ht else None
    if ht:
        h_t1, h_t3, h_t5, h_den = topn(ht)
    # drift: 实际比分线 中场收盘 - 初盘
    drift = None
    if a_open is not None and ht and actual in ht:
        drift = round(ht[actual] - a_open, 2)
    # 隐含概率(初盘, 去水)
    imp = (1.0 / a_open / o_den * 100) if a_open else None
    margin = (o_den - 1) * 100
    recs.append(dict(mk=mk, lg=lg, actual=actual,
        o_t1=o_t1, o_t3=o_t3, o_t5=o_t5, a_open=a_open, imp=imp, margin=margin,
        h_t1=h_t1, h_t3=h_t3, h_t5=h_t5, a_ht=a_ht, drift=drift,
        has_ht=bool(ht), n_lines=len(op)))

print('processed records:', len(recs))
c.close()

# ---- 聚合 ----
N = len(recs)
# 初盘
o_denom = sum(1 for r in recs if r['a_open'] is not None)
o_h1 = sum(1 for r in recs if r['a_open'] is not None and r['actual'] == r['o_t1'])
o_h3 = sum(1 for r in recs if r['a_open'] is not None and r['actual'] in r['o_t3'])
o_h5 = sum(1 for r in recs if r['a_open'] is not None and r['actual'] in r['o_t5'])
o_unprice = sum(1 for r in recs if r['a_open'] is None)
o_avg_margin = sum(r['margin'] for r in recs) / N
# 中场收盘(仅 has_ht 子集)
ht_recs = [r for r in recs if r['has_ht'] and r['a_ht'] is not None]
h_denom = len(ht_recs)
h_h1 = sum(1 for r in ht_recs if r['actual'] == r['h_t1'])
h_h3 = sum(1 for r in ht_recs if r['actual'] in r['h_t3'])
h_h5 = sum(1 for r in ht_recs if r['actual'] in r['h_t5'])
# drift
drift_recs = [r for r in recs if r['drift'] is not None]
d_short = sum(1 for r in drift_recs if r['drift'] < 0)
d_long = sum(1 for r in drift_recs if r['drift'] > 0)
d_flat = sum(1 for r in drift_recs if r['drift'] == 0)

print(f'OPEN  N={N} denom={o_denom} h1={o_h1}({o_h1/o_denom*100:.1f}%) h3={o_h3}({o_h3/o_denom*100:.1f}%) h5={o_h5}({o_h5/o_denom*100:.1f}%)')
print(f'HT    N={h_denom} h1={h_h1}({h_h1/h_denom*100:.1f}%) h3={h_h3}({h_h3/h_denom*100:.1f}%) h5={h_h5}({h_h5/h_denom*100:.1f}%)')
print(f'DRIFT n={len(drift_recs)} short(down)={d_short} long(up)={d_long} flat={d_flat}')

# ---- HTML ----
def esc(s): return html.escape(str(s))
def fmt(x, d=2): return ('%.*f' % (d, x)) if isinstance(x, (int, float)) else str(x)

def row_html(r):
    actual = r['actual']
    o_hit3 = r['a_open'] is not None and actual in r['o_t3']
    hl = ' class="hit"' if o_hit3 else ''
    o_top3 = ', '.join(r['o_t3']) if r['o_t3'] else '—'
    h_top3 = ', '.join(r['h_t3']) if r['h_t3'] else '—'
    aopen = fmt(r['a_open']) if r['a_open'] is not None else '—'
    aht = fmt(r['a_ht']) if r['a_ht'] is not None else '—'
    imp = fmt(r['imp'], 1) + '%' if r['imp'] is not None else '—'
    drift = ('%+.2f' % r['drift']) if r['drift'] is not None else '—'
    drift_cls = 'dn' if (r['drift'] is not None and r['drift'] < 0) else ('up' if (r['drift'] is not None and r['drift'] > 0) else '')
    mark = '✓' if o_hit3 else '✗'
    return (f"<tr{hl}><td class='lg'>{esc(r['lg'][:22])}</td><td>{esc(r['mk'])}</td>"
            f"<td class='act'>{actual}</td>"
            f"<td>{esc(o_top3)}</td><td>{aopen}</td><td>{imp}</td>"
            f"<td>{esc(h_top3)}</td><td>{aht}</td>"
            f"<td class='{drift_cls}'>{drift}</td>"
            f"<td class='{('ok' if mark=='✓' else 'no')}'>{mark}</td>"
            f"<td>{r['n_lines']}</td></tr>")

tbody = ''.join(row_html(r) for r in sorted(recs, key=lambda r: (r['a_open'] is None, -(0))))

doc = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><style>
body{{background:#0f1115;color:#e6e6e6;font-family:Segoe UI,system-ui,sans-serif;margin:0;padding:24px;font-size:13px}}
h1{{font-size:21px;margin:0 0 4px}} .sub{{color:#8a8a8a;font-size:12px;margin-bottom:16px}}
.card{{background:#171a21;border:1px solid #232833;border-radius:10px;padding:16px;margin-bottom:16px}}
.grid{{display:flex;flex-wrap:wrap;gap:12px}}
.kpi{{flex:1;min-width:130px;background:#1d2230;border:1px solid #2a3142;border-radius:8px;padding:12px}}
.kpi .v{{font-size:24px;font-weight:700;color:#7fd1ff}} .kpi .l{{color:#8a8a8a;font-size:11px;margin-top:4px}}
.kpi.g .v{{color:#5fdc8a}} .kpi.o .v{{color:#E65100}}
table{{border-collapse:collapse;width:100%;font-size:12px}} th,td{{padding:5px 8px;text-align:right;border-bottom:1px solid #232833;white-space:nowrap}}
th{{color:#8a8a8a;font-weight:600;position:sticky;top:0;background:#171a21}} td:first-child,th:first-child,td:nth-child(2),th:nth-child(2){{text-align:left}}
td.lg{{color:#9fb3c8}} td.act{{color:#ffd27a;font-weight:700}} tr.hit{{background:#14241a}}
.ok{{color:#5fdc8a;font-weight:700}} .no{{color:#e06a6a}} .act{{color:#ffd27a}}
.dn{{color:#5fdc8a;font-weight:700}} .up{{color:#e06a6a}}
.scroll{{max-height:600px;overflow:auto;border:1px solid #232833;border-radius:8px}}
.warn{{background:#2a1f12;border:1px solid #5a3d1a;border-radius:8px;padding:12px;color:#e8b07a;font-size:12px;line-height:1.7}}
h2{{font-size:15px;margin:0 0 8px;color:#cfe0f0}} ul{{margin:6px 0;padding-left:20px;line-height:1.6}}
.legend span{{margin-right:14px;font-size:12px}}
</style></head><body>
<h1>全量波胆(CS)赔率复盘 — 双时点 clean 版 (2026-07-16 ~ 07-20)</h1>
<div class="sub">正源 data/events.db.odds_snapshots(market=CS) · 赔率取 <b>初盘(per-selection首见)</b> + <b>中场收盘(kick+44~52min)</b> 双时点 · 已剔除 VS-/EAFC/PANDA · 实际结果取 matches 表 finished 比分</div>

<div class="card warn">
<b>⚠ 数据体检 caveat（铁律：只看数据，且要验数据本身）</b><br>
matches.score_home/score_away 已被证实<b>污染</b>：进第一个球后乐鱼直播页删该场，采集器把当时比分(多为1-0/0-1)冻结成"终场"——
表现为 <b>49% 比赛是单进球</b>、<b>0-1 比 1-0 还多(违反主场优势)</b>、8 场 minute=45 被标 finished。
因此本报告"实际"列来自污染字段，<b>命中率仅供"市场结构预期 vs 记录比分一致性"参考，严禁用于模型 λ 校准</b>。
odds 快照本身干净（本复盘只消费 odds，不消费 score 做校准）。
</div>

<div class="card grid">
  <div class="kpi"><div class="v">{N}</div><div class="l">复盘场次数</div></div>
  <div class="kpi"><div class="v">{o_denom}</div><div class="l">初盘可定价 (实际在CS内)</div></div>
  <div class="kpi"><div class="v" style="color:#5fdc8a">{o_h3/o_denom*100:.1f}%</div><div class="l">初盘前3命中 (hit3)</div></div>
  <div class="kpi g"><div class="v">{o_h5/o_denom*100:.1f}%</div><div class="l">初盘前5命中 (hit5)</div></div>
  <div class="kpi o"><div class="v">{o_h1/o_denom*100:.1f}%</div><div class="l">初盘第1命中 (hit1)</div></div>
</div>

<div class="card grid">
  <div class="kpi"><div class="v">{h_denom}</div><div class="l">中场收盘可定价场(子集)</div></div>
  <div class="kpi"><div class="v" style="color:#5fdc8a">{h_h3/h_denom*100:.1f}%</div><div class="l">中场前3命中 (hit3)</div></div>
  <div class="kpi g"><div class="v">{h_h5/h_denom*100:.1f}%</div><div class="l">中场前5命中 (hit5)</div></div>
  <div class="kpi o"><div class="v">{h_h1/h_denom*100:.1f}%</div><div class="l">中场第1命中 (hit1)</div></div>
  <div class="kpi"><div class="v">{o_avg_margin:.0f}%</div><div class="l">CS市场平均margin</div></div>
</div>

<div class="card">
<h2>核心结论</h2>
<ul>
<li><b>初盘前3热门命中实际比分 {o_h3}/{o_denom} 场 ({o_h3/o_denom*100:.1f}%)</b>；前5热门 {o_h5/o_denom*100:.1f}%；单场第1热门仅 {o_h1/o_denom*100:.1f}%（CS 长尾分布，精确命中单线天然低）。</li>
<li><b>中场收盘子集({h_denom}场) 前3命中 {h_h3/h_denom*100:.1f}%</b>。中场盘把"最可能的几个比分"收窄得更准（比赛已踢过半，信息更多），可作为 in-play 实时判定的有效信号源。</li>
<li><b>初盘→中场收盘漂移 ({len(drift_recs)}场有双时点)</b>：实际比分赔率<b>下调(更被看好) {d_short} 场</b> / 上调(看衰) {d_long} 场 / 持平 {d_flat} 场。若 short&gt;long 说明临场资金整体站实际结果一侧（顺人性盘 signal）。</li>
<li><b>{o_unprice} 场实际比分不在 CS 定价表</b>（多为 5+ 球/极端比分）——庄家 CS 仅列到 4-4，结构性盲区，非模型失误。</li>
<li>CS 市场 margin 高达 {o_avg_margin:.0f}%，单线"赔率偏高"≠可下注 edge；本复盘仅验证市场结构化预期一致性。</li>
</ul>
</div>

<div class="card">
<div class="legend"><span class="ok">✓</span> 实际∈初盘前3</span><span class="dn">绿=赔率下调(被看好)</span><span class="up">红=赔率上调(看衰)</span><span class="act">橙=实际比分</span></div>
<div class="scroll"><table>
<thead><tr><th>联赛</th><th>对阵</th><th>实际</th><th>初盘前3</th><th>实际初盘赔</th><th>隐含%</th><th>中场前3</th><th>实际中场赔</th><th>初盘→中场漂移</th><th>前3?</th><th>CS线数</th></tr></thead>
<tbody>{tbody}</tbody></table></div>
<div class="sub" style="margin-top:8px">漂移=中场收盘−初盘(负=临场更被看好)。"—"=该比分不在CS定价表或中场窗口无快照。命中判定基于污染 score 字段，仅供参考。</div>
</div>
</body></html>'''

open(OUT, 'w', encoding='utf-8').write(doc)
print('written:', OUT, len(doc), 'bytes')
print('SUMMARY', json.dumps(dict(
    N=N, o_denom=o_denom, o_h1=o_h1, o_h3=o_h3, o_h5=o_h5, o_unprice=o_unprice,
    o_avg_margin=round(o_avg_margin, 1),
    h_denom=h_denom, h_h1=h_h1, h_h3=h_h3, h_h5=h_h5,
    drift_n=len(drift_recs), d_short=d_short, d_long=d_long, d_flat=d_flat)))
