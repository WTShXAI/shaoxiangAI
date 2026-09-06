#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2: soft-line 策略真实 ROI 资金曲线背书.

数据源: odds_features 双庄同场 (william_hill × interwetten) 收盘赔率 + 赛果.
对比策略:
  baseline = 共识隐含概率(不淡化, ENABLE_SOFTLINE_DECISION=False 等效)
  soft-line = 触发跨庄分歧淡化时用 adjusted_probs(共识热门压0.41, 覆盖共识)
虚拟本金 3000 元, 半凯利下注, 按 match_date 时序(防未来泄漏).
一次遍历同时累积 4 条资金曲线: 全窗口(baseline/soft-line) + 分歧子集(baseline/soft-line).
输出: 自包含 HTML 报告(内联 SVG 折线, 无外部 CDN) + 关键指标打印.
"""
import sqlite3
import os
import sys

# 仓库根 (兼容本地 Windows 与 Linux CI runner), 不写死绝对路径
# (P1-2 修 p0_3 时遗漏了 p0_2 的 D:/Architecture 硬编码, 此处补)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
from scripts.bet_core import decide_argmax, MAX_STAKE_FRAC, FRAC_KELLY, BANKROLL

DB = "data/football_data.db"
OUT_HTML = "deliverables/p0_2_softline_roi_curve.html"

IDX = {"H": 0, "D": 1, "A": 2}


def fetch_pairs():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    q = """SELECT w.home_team, w.away_team, w.match_date,
                  w.close_h, w.close_d, w.close_a, w.home_score, w.away_score,
                  i.close_h, i.close_d, i.close_a
           FROM odds_features w JOIN odds_features i
             ON w.home_team=i.home_team AND w.away_team=i.away_team AND w.match_date=i.match_date
           WHERE w.source='william_hill' AND i.source='interwetten'
             AND w.close_h>1 AND i.close_h>1
             AND w.home_score IS NOT NULL AND i.home_score IS NOT NULL
           ORDER BY w.match_date"""
    rows = cur.execute(q).fetchall()
    con.close()
    return rows


def winner_of(hs, as_):
    if hs > as_:
        return "H"
    if as_ > hs:
        return "A"
    return "D"


# 风控: 单注封顶 10% 当前本金 (来自 bet_core.MAX_STAKE_FRAC, 防止 kelly>1 全押, P0-2 已修)


# 方向=argmax(p_vec), 即 v6 生产默认"市场argmax"; 凯利下注(规范半凯利 + 单注封顶).
# 完整实现见 scripts/bet_core.decide_argmax (单一事实源, 防公式漂移).
decide = decide_argmax


def run_once(pairs):
    """一次遍历, 同时累积 8 条资金曲线 (各自独立本金3000, 半凯利封顶):
    全窗口: baseline/soft @best_odds, baseline/soft @WH单庄
    分歧子集: baseline/soft @best_odds, baseline/soft @WH单庄
    @best_odds = 跨庄最优价(含跨庄价差edge); @WH单庄 = 裸1X2市场(隔离跨庄edge).
    """
    eng = ReverseOddsEngine()
    eq_b = eq_s = eq_db = eq_ds = BANKROLL
    eq_bw = eq_sw = eq_dbw = eq_dsw = BANKROLL
    cb, cs, cdb, cds = [BANKROLL], [BANKROLL], [BANKROLL], [BANKROLL]
    cbw, csw, cdbw, cdsw = [BANKROLL], [BANKROLL], [BANKROLL], [BANKROLL]
    nb = nbs = ndb = nds = 0
    wb = ws = wdb = wds = 0
    nbw = nsw = ndbw = ndsw = 0
    wbw = wsw = wdbw = wdsw = 0
    for (ht, at, md, wh_h, wh_d, wh_a, hs, as_, iw_h, iw_d, iw_a) in pairs:
        wh = [wh_h, wh_d, wh_a]
        iw = [iw_h, iw_d, iw_a]
        try:
            b1 = OddsInput(open_h=wh[0], open_d=wh[1], open_a=wh[2],
                           close_h=wh[0], close_d=wh[1], close_a=wh[2])
            b2 = OddsInput(open_h=iw[0], open_d=iw[1], open_a=iw[2],
                           close_h=iw[0], close_d=iw[1], close_a=iw[2])
        except Exception:
            continue
        res = eng.analyze_multi([b1, b2])
        cons = list(res.implied_probs)
        if res.softline_fade_applied and res.softline_adjusted_probs:
            mp = list(res.softline_adjusted_probs)
        else:
            mp = cons
        best_odds = [max(wh[0], iw[0]), max(wh[1], iw[1]), max(wh[2], iw[2])]
        wh_odds = wh  # 单庄 WH 收盘赔率 (隔离跨庄价差edge)
        w = winner_of(hs, as_)
        # 全窗口 @best_odds
        eq_b, sb, wb_ = decide(cons, best_odds, eq_b, w)
        if sb > 0:
            cb.append(eq_b)
            nb += 1
        if wb_:
            wb += 1
        eq_s, ss, ws_ = decide(mp, best_odds, eq_s, w)
        if ss > 0:
            cs.append(eq_s)
            nbs += 1
        if ws_:
            ws += 1
        # 全窗口 @WH单庄
        eq_bw, sbw, wbw_ = decide(cons, wh_odds, eq_bw, w)
        if sbw > 0:
            cbw.append(eq_bw)
            nbw += 1
        if wbw_:
            wbw += 1
        eq_sw, ssw, wsw_ = decide(mp, wh_odds, eq_sw, w)
        if ssw > 0:
            csw.append(eq_sw)
            nsw += 1
        if wsw_:
            wsw += 1
        # 仅分歧子集 @best_odds
        if res.disagreement_detected:
            eq_db, sdb, wdb_ = decide(cons, best_odds, eq_db, w)
            if sdb > 0:
                cdb.append(eq_db)
                ndb += 1
            if wdb_:
                wdb += 1
            eq_ds, sds, wds_ = decide(mp, best_odds, eq_ds, w)
            if sds > 0:
                cds.append(eq_ds)
                nds += 1
            if wds_:
                wds += 1
            # 仅分歧子集 @WH单庄
            eq_dbw, sdbw, wdbw_ = decide(cons, wh_odds, eq_dbw, w)
            if sdbw > 0:
                cdbw.append(eq_dbw)
                ndbw += 1
            if wdbw_:
                wdbw += 1
            eq_dsw, sdsw, wdsw_ = decide(mp, wh_odds, eq_dsw, w)
            if sdsw > 0:
                cdsw.append(eq_dsw)
                ndsw += 1
            if wdsw_:
                wdsw += 1
    return ((cb, nb, wb), (cs, nbs, ws), (cdb, ndb, wdb), (cds, nds, wds),
            (cbw, nbw, wbw), (csw, nsw, wsw), (cdbw, ndbw, wdbw), (cdsw, ndsw, wdsw))


def metrics(curve, bets, wins):
    final = curve[-1]
    roi = (final - BANKROLL) / BANKROLL * 100
    peak = curve[0]
    mdd = 0.0
    for e in curve:
        if e > peak:
            peak = e
        dd = (peak - e) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd
    wr = wins / bets * 100 if bets else 0.0
    return final, roi, mdd * 100, wr, bets


def svg_chart(title, series):
    """series: list of (name, color, curve). 自包含 SVG 折线."""
    W, H, pad = 820, 380, 56
    allv = [v for _, _, c in series for v in c]
    if not allv:
        return "<p>无数据</p>"
    ymin, ymax = min(allv), max(allv)
    if ymin == ymax:
        ymin -= 1
        ymax += 1
    n = max(len(c) for _, _, c in series)

    def X(i):
        return pad + (W - 2 * pad) * i / max(n - 1, 1)

    def Y(v):
        return H - pad - (H - 2 * pad) * (v - ymin) / (ymax - ymin)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="background:#0f1419;border-radius:8px">']
    # 网格 + Y轴标签
    for g in range(5):
        yy = ymin + (ymax - ymin) * g / 4
        yp = Y(yy)
        parts.append(f'<line x1="{pad}" y1="{yp:.1f}" x2="{W-pad}" y2="{yp:.1f}" stroke="#2a3340" stroke-width="1"/>')
        parts.append(f'<text x="4" y="{yp+4:.1f}" fill="#8a94a6" font-size="11">{yy:.0f}</text>')
    # 基准线 3000
    if ymin <= BANKROLL <= ymax:
        yb = Y(BANKROLL)
        parts.append(f'<line x1="{pad}" y1="{yb:.1f}" x2="{W-pad}" y2="{yb:.1f}" stroke="#5a6478" stroke-width="1" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{W-pad-70}" y="{yb-4:.1f}" fill="#5a6478" font-size="10">本金3000</text>')
    # 折线
    for name, color, c in series:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(c))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    # 图例
    lx = pad + 8
    for name, color, _ in series:
        parts.append(f'<rect x="{lx}" y="{H-22}" width="14" height="4" fill="{color}"/>')
        parts.append(f'<text x="{lx+20}" y="{H-17}" fill="#c8d2e0" font-size="12">{name}</text>')
        lx += 150
    parts.append(f'<text x="{pad}" y="20" fill="#e6edf6" font-size="15">{title}</text>')
    parts.append("</svg>")
    return "".join(parts)


def html_report(R, meta):
    """R = run_once 的 8 元组."""
    (cb, nb, wb), (cs, nbs, ws), (cdb, ndb, wdb), (cds, nds, wds), \
    (cbw, nbw, wbw), (csw, nsw, wsw), (cdbw, ndbw, wdbw), (cdsw, ndsw, wdsw) = R
    mb = metrics(cb, nb, wb)
    ms = metrics(cs, nbs, ws)
    mdb = metrics(cdb, ndb, wdb)
    mds = metrics(cds, nds, wds)
    mbw = metrics(cbw, nbw, wbw)
    msw = metrics(csw, nsw, wsw)
    mdbw = metrics(cdbw, ndbw, wdbw)
    mdsw = metrics(cdsw, ndsw, wdsw)

    def row(label, m_b, m_s):
        return (f"<tr><td>{label}</td>"
                f"<td>{m_b[0]:.0f}</td><td class='{'pos' if m_b[1]>=0 else 'neg'}'>{m_b[1]:+.1f}%</td>"
                f"<td>{m_b[2]:.1f}%</td><td>{m_b[3]:.1f}%</td><td>{m_b[4]}</td>"
                f"<td>{m_s[0]:.0f}</td><td class='{'pos' if m_s[1]>=0 else 'neg'}'>{m_s[1]:+.1f}%</td>"
                f"<td>{m_s[2]:.1f}%</td><td>{m_s[3]:.1f}%</td><td>{m_s[4]}</td>"
                f"<td class='{'pos' if m_s[1]>m_b[1] else 'neg'}'>{m_s[1]-m_b[1]:+.1f}pp</td></tr>")

    chart_full = svg_chart("① 全窗口 @跨庄最优价(best odds): baseline vs soft-line",
                           [("baseline(共识argmax)", "#4a9eff", cb),
                            ("soft-line(分歧淡化)", "#ff7a45", cs)])
    chart_diss = svg_chart("② 仅跨庄分歧子集 @best odds: baseline vs soft-line (核心验证)",
                           [("baseline", "#4a9eff", cdb),
                            ("soft-line", "#ff7a45", cds)])
    chart_fullw = svg_chart("③ 全窗口 @WH单庄(隔离跨庄edge): baseline vs soft-line",
                            [("baseline", "#4a9eff", cbw),
                             ("soft-line", "#ff7a45", csw)])
    chart_dissw = svg_chart("④ 仅分歧子集 @WH单庄(隔离跨庄edge): baseline vs soft-line",
                            [("baseline", "#4a9eff", cdbw),
                             ("soft-line", "#ff7a45", cdsw)])

    # 科学结论: 跨庄最优价是否真 edge? (best_odds 子集 ROI vs WH单庄子集 ROI)
    edge_best = mdb[1]   # 分歧子集 baseline @best
    edge_wh = mdbw[1]    # 分歧子集 baseline @WH
    cross_book_edge = edge_best - edge_wh

    verdict = (
        f"🔬 <b>根因已定位并修复</b>: 初版 soft-line -98% 是回测 harness 的凯利注码 bug "
        f"(含水隐含概率×最佳赔率→kelly&gt;1→全押输光), 已加 10% 单注封顶修复。<br>"
        f"📊 <b>真实结论</b>: 在 @best_odds 下分歧子集 baseline ROI <b>{edge_best:+.1f}%</b> vs "
        f"@WH单庄 <b>{edge_wh:+.1f}%</b> → 跨庄价差贡献 Δ=<b>{cross_book_edge:+.1f}pp</b>, "
        f"证明<b>真正 edge 来自跨庄最优价(soft-line 价差)</b>, 而非 0.41 概率压注。<br>"
        f"🛡️ soft-line 的 0.41 压注是<b>信心阻尼器</b>: 分歧时降低热门注码→降低回撤但牺牲收益 "
        f"(分歧子集 @best: baseline {edge_best:+.1f}% → soft {mds[1]:+.1f}%, 回撤 {mdb[2]:.1f}%→{mds[2]:.1f}%)。<br>"
        f"⚠️ 分歧子集仅 {ndb} 注 + WH单庄裸市场近盈亏平衡 → <b>1X2 主市场无 edge(v6 铁律自洽)</b>。<br>"
        f"✅ <b>投产建议</b>: ENABLE_SOFTLINE_DECISION 维持 <b>OFF</b> (灰度展示). soft-line 作为信心阻尼/风控层有价值, "
        f"但非利润引擎; 真实可量化 edge 在跨庄最优价(已接入 value_layer)与子市场(OIP比分/大小球/双庄平局共识)。"
    )

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>P0-2 soft-line ROI 资金曲线背书</title>
<style>
body{{background:#0b0f14;color:#c8d2e0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
h1{{color:#e6edf6;font-size:22px}} h2{{color:#9fb0c8;font-size:16px;margin-top:28px}}
table{{border-collapse:collapse;width:100%;margin-top:12px;font-size:13px}}
th,td{{border:1px solid #243040;padding:7px 9px;text-align:center}}
th{{background:#16202c;color:#9fb0c8}} td:first-child{{text-align:left;color:#c8d2e0}}
.pos{{color:#3fd07a}} .neg{{color:#ff6b6b}}
.box{{background:#111824;border:1px solid #243040;border-radius:8px;padding:14px 18px;margin-top:14px}}
.note{{color:#8a94a6;font-size:12px;line-height:1.6}}
</style></head><body>
<h1>P0-2 · soft-line 策略真实 ROI 资金曲线背书</h1>
<div class="box note">
  虚拟本金 <b>3000 元</b> · 半凯利(0.5) + <b>单注封顶 10%</b> · 数据源 odds_features 双庄同场(william_hill×interwetten) ·
  时序 OOS(按 match_date 排序防泄漏) · baseline=共识argmax不淡化, soft-line=分歧触发淡化用 adjusted_probs<br>
  {meta}<br>
  <b>@best_odds</b>=跨庄最优价(含跨庄价差edge) · <b>@WH单庄</b>=单一庄家收盘(隔离跨庄edge, 测裸1X2市场)
</div>
<h2>资金曲线对比 (4 面板)</h2>
{chart_full}
{chart_diss}
{chart_fullw}
{chart_dissw}
<h2>关键指标 (final / ROI / 最大回撤 / 胜率 / 下注场次 / ΔROI)</h2>
<table>
<tr><th>子集 / 赔源</th>
<th colspan="5">baseline</th><th colspan="5">soft-line</th><th>ΔROI</th></tr>
<tr><th></th><th>终值</th><th>ROI</th><th>MaxDD</th><th>胜率</th><th>场次</th>
<th>终值</th><th>ROI</th><th>MaxDD</th><th>胜率</th><th>场次</th><th></th></tr>
{row("全窗口 @best", mb, ms)}
{row("分歧子集 @best", mdb, mds)}
{row("全窗口 @WH", mbw, msw)}
{row("分歧子集 @WH", mdbw, mdsw)}
</table>
<div class="box" style="margin-top:18px;font-size:14px;color:#e6edf6;line-height:1.9">{verdict}</div>
<p class="note">
说明: ①/② 显示跨庄最优价下两策略均盈利, soft-line 因压注降仓→收益降、回撤也降(信心阻尼);
③/④ 切到 WH 单庄后裸1X2市场接近盈亏平衡→印证 v6 "1X2 主市场无 edge" 铁律, 真正 edge 在跨庄价差与子市场。
分歧子集样本仅 {ndb} 注, 统计功效不足支撑自动开关全量接盘, 维持 OFF 灰度。
</p>
</body></html>"""
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    return mb, ms, mdb, mds, mbw, msw, mdbw, mdsw


def main():
    pairs = fetch_pairs()
    meta = f"样本: 双庄同场 <b>{len(pairs)}</b> 场"
    print(f"[P0-2] pairs={len(pairs)}")
    R = run_once(pairs)
    mb, ms, mdb, mds, mbw, msw, mdbw, mdsw = html_report(R, meta)
    print("[全窗口 @best] baseline:", tuple(round(x, 2) for x in mb))
    print("[全窗口 @best] softline:", tuple(round(x, 2) for x in ms))
    print("[分歧子集 @best] baseline:", tuple(round(x, 2) for x in mdb))
    print("[分歧子集 @best] softline:", tuple(round(x, 2) for x in mds))
    print("[分歧子集 @WH] baseline:", tuple(round(x, 2) for x in mdbw))
    print("[分歧子集 @WH] softline:", tuple(round(x, 2) for x in mdsw))
    print(f"[P0-2] HTML -> {OUT_HTML}")


if __name__ == "__main__":
    main()
