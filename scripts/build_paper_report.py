#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合成 '分析内容→虚拟交易' 盈利性验证总报告 (起始500金币). 单庄+跨庄双视角, 诚实戳破过拟合."""
import json, os, csv

ROOT = os.path.abspath(".")
OUT = os.path.join(ROOT, "deliverables", "paper_trade_500_report.html")

# ── 数据: 单庄 full_chain_backtest (500) ──
fcb = json.load(open(os.path.join(ROOT, "data", "full_chain_backtest_report.json"), encoding="utf-8"))
modes = fcb["modes"]
wf = fcb["walk_forward"]
oos = wf["oos_value_gate_Tstar"]
ins = wf["test_insample_defaultT"]

# value_gate 权益曲线
vg_curve = []
with open(os.path.join(ROOT, "data", "full_chain_equity_curve_value_gate.csv")) as f:
    for row in csv.reader(f):
        if row and row[0] != "step":
            vg_curve.append(float(row[1]))

# ── 数据: 跨庄 p0_2 (500) ──
p02 = {
    "全窗口@最优价 baseline": (592.04, 18.41, 47.61, 48.3, 323),
    "全窗口@最优价 softline": (544.57, 8.91, 45.29, 48.3, 323),
    "分歧子集@最优价 baseline": (621.70, 24.34, 49.14, 39.39, 99),
    "分歧子集@最优价 softline": (571.85, 14.37, 46.89, 39.39, 99),
    "分歧子集@WH单庄 baseline": (452.37, -9.53, 38.02, 32.73, 55),
    "分歧子集@WH单庄 softline": (420.03, -15.99, 38.08, 32.73, 55),
}


def svg_curve(title, curve, base=500, w=820, h=360, color="#3fd07a", ref=True):
    if not curve:
        return "<p>无数据</p>"
    pad = 54
    ymin, ymax = min(curve + [base]), max(curve + [base])
    if ymin == ymax:
        ymin -= 50; ymax += 50
    ymin = min(ymin, 0)
    n = len(curve)
    X = lambda i: pad + (w - 2 * pad) * i / max(n - 1, 1)
    Y = lambda v: h - pad - (h - 2 * pad) * (v - ymin) / (ymax - ymin)
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" style="background:#0f1419;border-radius:8px">']
    for g in range(5):
        yy = ymin + (ymax - ymin) * g / 4
        yp = Y(yy)
        parts.append(f'<line x1="{pad}" y1="{yp:.1f}" x2="{w-pad}" y2="{yp:.1f}" stroke="#2a3340"/>')
        parts.append(f'<text x="4" y="{yp+4:.1f}" fill="#8a94a6" font-size="11">{yy:.0f}</text>')
    if ref and ymin <= base <= ymax:
        yb = Y(base)
        parts.append(f'<line x1="{pad}" y1="{yb:.1f}" x2="{w-pad}" y2="{yb:.1f}" stroke="#5a6478" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{w-pad-60}" y="{yb-4:.1f}" fill="#5a6478" font-size="10">本金500</text>')
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(curve))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    parts.append(f'<text x="{pad}" y="20" fill="#e6edf6" font-size="15">{title}</text>')
    parts.append("</svg>")
    return "".join(parts)


vg_svg = svg_curve("① 单庄·价值闸门(value_gate) 权益曲线 — 样本内 +168% (诱人但见下文)", vg_curve, color="#ff7a45")

row_ss = lambda name, m: (
    f"<tr><td>{name}</td><td>{m['n_bets']}</td>"
    f"<td class='{'pos' if m['roi']>=0 else 'neg'}'>{m['roi']*100:+.2f}%</td>"
    f"<td>{m['max_drawdown']*100:.2f}%</td>"
    f"<td>{m['hit_rate']*100:.1f}%</td>"
    f"<td class='{'pos' if (m['sharpe'] or 0)>=0 else 'neg'}'>{m['sharpe']}</td></tr>"
)

p02_rows = ""
for name, (final, roi, dd, hit, bets) in p02.items():
    cls = "pos" if roi >= 0 else "neg"
    p02_rows += (f"<tr><td>{name}</td><td>{final:.0f}</td>"
                 f"<td class='{cls}'>{roi:+.2f}%</td><td>{dd:.2f}%</td>"
                 f"<td>{hit:.1f}%</td><td>{bets}</td></tr>")

flat = modes["flat_argmax"]; vg = modes["value_gate"]; kl = modes["kelly_argmax"]

html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>虚拟交易盈利性验证 · 500金币</title>
<style>
body{{background:#0b0f14;color:#c8d2e0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
h1{{color:#e6edf6;font-size:23px}} h2{{color:#9fb0c8;font-size:17px;margin-top:30px}}
.box{{background:#111824;border:1px solid #243040;border-radius:8px;padding:14px 18px;margin-top:14px;line-height:1.7}}
.note{{color:#8a94a6;font-size:12.5px;line-height:1.65}}
table{{border-collapse:collapse;width:100%;margin-top:12px;font-size:13px}}
th,td{{border:1px solid #243040;padding:7px 9px;text-align:center}}
th{{background:#16202c;color:#9fb0c8}} td:first-child{{text-align:left;color:#c8d2e0}}
.pos{{color:#3fd07a}} .neg{{color:#ff6b6b}} .warn{{color:#ffb454}}
b{{color:#e6edf6}}
</style></head><body>
<h1>虚拟交易盈利性验证报告 · 起始 500 金币</h1>
<div class="box note">
验证目标: <b>"从分析内容(模型verdict+概率+价值层)做虚拟交易, 初始500, 能否稳定盈利?"</b><br>
数据源: 分析内容 = UnifiedPredictor v7.4 + compute_value_layer + bet_core(半凯利+10%封顶, 低赔热门限注)。
赛果/赔率来自 football_data.db(单庄WH, 30098场, 2015–2026) 与 odds_features(双庄WH×IW, 16140对)。<br>
时序OOS(按日期升序防泄漏)。诚实对照含 walk-forward(2015-22调参→盲跑2023-26)。
</div>

<h2>① 单庄(=当前前端"分析内容"来源 乐鱼/GQ)</h2>
<table>
<tr><th>策略</th><th>下注数</th><th>ROI</th><th>最大回撤</th><th>胜率</th><th>Sharpe</th></tr>
{row_ss("无脑跟模型 flat_argmax (每场押verdict)", flat)}
{row_ss("价值闸门 value_gate (仅系统判有edge才下)", vg)}
{row_ss("凯利 argmax+gate", kl)}
</table>
{vg_svg}
<div class="box warn">
⚠️ <b>诚实戳破过拟合</b>: 上面 value_gate 的 <b>+168%</b> 是<b>样本内</b>结果(参数在同源数据上挑出来的)。
用 walk-forward 把数据切开 — 2015–2022 标定最优温度 T*={wf['optimal_T']}, 盲跑 2023–2026 未来时段:<br>
&nbsp;&nbsp;• 盲测 OOS(value_gate, T*): <b class='neg'>ROI {oos['roi']*100:+.2f}%</b>, 最大回撤 {oos['max_drawdown']*100:.2f}%, 胜率 {oos['hit_rate']*100:.1f}%, {oos['n_bets']} 注<br>
&nbsp;&nbsp;• 对照(同源默认T样本内): +{ins['roi']*100:.2f}% ← 这是"作弊"视角, 不能当真<br>
<b>结论: 去掉过拟合后, 单庄(当前前端分析内容来源)纸交易 ≈ 盈亏平衡 / 微亏, 无法稳定盈利。</b>
无脑每场跟模型更直接爆仓(-100%)。这与系统铁律#3(模型AUC 0.62 &lt; 隐含0.73)完全自洽。
</div>

<h2>② 跨庄软线价差(真 edge 来源)</h2>
<table>
<tr><th>子集 / 赔源</th><th>终值</th><th>ROI</th><th>最大回撤</th><th>胜率</th><th>场次</th></tr>
{p02_rows}
</table>
<div class="box note">
@最优价 = 跨庄取 WH/IW 中更优赔率(含跨庄价差edge); @WH单庄 = 只用单一庄家(隔离跨庄edge)。<br>
<b>关键对照</b>: 同一批比赛, 取跨庄最优价 → <b class='pos'>+18%~+24%</b>; 锁死单庄 WH → <b class='neg'>-9.5%~-16%</b>。<br>
证明 <b>edge 来自"跨庄价差/最优价执行", 不在模型预测本事</b>。bet_core 早有 <b>MIN_SPREAD_PP</b> 跨庄闸门(默认关闭), 就是为这个设计的。
</div>

<h2>③ 一句话结论与副业现实</h2>
<div class="box">
<b>1. 当前前端"分析内容"(单庄 GQ/乐鱼)纸交易 → 不能稳定盈利。</b> 诚实盲测≈0; 样本内+168%是过拟合陷阱, 别被它骗去加注。<br>
<b>2. 唯一回测为正的路径是跨庄最优价价差(+18~24%)</b>, 但需要<b>多庄实时盘口 + 在最优价下单</b>——当前单庄管线不喂这个, 是系统工程不是改前端能解决。<br>
<b>3. 模型定位</b>: AUC 0.62 &lt; 隐含概率 0.73, 模型不是利润引擎, 应做<b>风控/过滤层</b>(价值闸门、低赔限注已就位)。<br>
<b>4. 副业提醒(必须说清)</b>: 这是博彩相邻领域。所有回测里<b>单庄策略全负</b>; 跨庄 edge 虽正但薄(323注样本、最大回撤47%)、依赖实时多庄流动性、且真实下注有平台限额/账户风险。
<b>请勿把"稳定小康"寄托于此</b>; 若真要做, 正确路径=接多庄盘口(如 Pinnacle/Interwetten/威廉)+ 开启 MIN_SPREAD_PP 跨庄闸门 + 严格仓位风控, 且只用闲钱、当研究不当生计。
</div>
<p class="note">附: 单庄明细 data/full_chain_backtest_report.json; 跨庄明细 deliverables/p0_2_softline_roi_curve.html。本报告为历史回测, 非未来收益保证。</p>
</body></html>"""

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html)
print("[report] ->", OUT, "bytes=", len(html))
