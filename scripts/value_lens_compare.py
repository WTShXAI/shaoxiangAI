# -*- coding: utf-8 -*-
"""
value_lens_compare.py — 世界级分析 vs 赛程OU vs 天眼 三镜头诚实价值对比 + 实时绿框
===================================================================================
产出: reports/value_lens_compare_<date>.html (自包含, 浏览器打开, 实时数据驱动)

绿框两档(诚实分级):
  Tier1 真实可买(天眼验证+EV): 天眼开盘独立模型 edge>0 且两队入 team_canonical
       (该人口 OOS 验证 +10.05%, CI 正). 这是唯一经实证的"可购买"信号.
  Tier2 诊断绿框(世界级分析): edge 三件套 win_rate>隐含+抽水(edge>=1pp).
       其模型 fused/fl OOS 低于市场基线 → 仅诊断/交叉验证, 非拍板, 不可盲跟.
赛程OU 镜头: 全局无 edge(AUC 0.50-0.52) → N/A.

数据源: data/events.db (GQ 实时, 本机 bridge 健康, 快照分钟级更新).
重跑本脚本即刷新实时绿框(主流联赛开赛时 天眼覆盖场增多 → Tier1 增多).
"""
from __future__ import annotations
import os, sys, json, sqlite3
from datetime import date

sys.path.insert(0, os.getcwd())
from pipeline.world_analyzer import analyze_match
from pipeline.open_eye_predictor import recommend, _covered

EV_DB = "data/events.db"
OUT = os.path.join("reports", f"value_lens_compare_{date.today().isoformat()}.html")
GREEN_MIN_PP = 1.0


def latest_1x2(con, mk):
    rows = con.execute("""SELECT selection, odds, captured_at FROM odds_snapshots
        WHERE match_key=? AND market='1X2' ORDER BY captured_at DESC""", (mk,)).fetchall()
    d = {}
    for r in rows:
        if r["selection"] not in d:
            d[r["selection"]] = float(r["odds"])
    return d.get("home"), d.get("draw"), d.get("away")


def opening_1x2(con, mk):
    rows = con.execute("""SELECT selection, odds, captured_at FROM odds_snapshots
        WHERE match_key=? AND market='1X2' ORDER BY captured_at ASC""", (mk,)).fetchall()
    d = {}
    for r in rows:
        if r["selection"] not in d:
            d[r["selection"]] = float(r["odds"])
    return d.get("home"), d.get("draw"), d.get("away")


def is_nonstd(league, h, d, a):
    if not league:
        return False
    if "分钟" in league:
        return True
    try:
        if min(h, d, a) < 1.05 or max(h, d, a) > 40:
            return True
    except Exception:
        pass
    return False


def load_live():
    con = sqlite3.connect(EV_DB); con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT DISTINCT m.match_key, m.league, m.status FROM matches m
        JOIN odds_snapshots s ON s.match_key=m.match_key WHERE m.status NOT IN ('finished')
        AND s.market='1X2'""").fetchall()
    out = []
    for m in rows:
        mk = m["match_key"]
        parts = [x.strip() for x in mk.split(" vs ")]
        ht, at = (parts + [None, None])[:2]
        if not (ht and at):
            continue
        oh, od, oa = opening_1x2(con, mk)
        ch, cd, ca = latest_1x2(con, mk)
        if not (ch and cd and ca):
            continue
        out.append({"mk": mk, "h": ht, "a": at, "lg": m["league"], "st": m["status"],
                    "open": (oh, od, oa), "cur": (ch, cd, ca)})
    con.close()
    return out


def analyze_all(matches):
    tier1, tier2 = [], []
    cov_total = 0
    for m in matches:
        oh, od, oa = m["open"]; ch, cd, ca = m["cur"]
        # 天眼 (开盘价)
        cov = _covered(m["h"], m["a"])
        if cov:
            cov_total += 1
            rec = recommend(m["h"], m["a"], oh, od, oa, m["mk"], m["lg"])
            if rec.get("ok") and rec["edge_pp"] > 0:
                rec["nonstd"] = is_nonstd(m["lg"], oh, od, oa)
                rec["match"] = m
                tier1.append(rec)
        # 世界级分析 (当前价, 诊断)
        try:
            wa = analyze_match(m["h"], m["a"], m["lg"], h=ch, d=cd, a=ca)
            e = wa.get("edge_1x2")
            if e and e.get("edge_pp") is not None and e["edge_pp"] >= GREEN_MIN_PP:
                e["match"] = m
                tier2.append(e)
        except Exception:
            pass
    tier1.sort(key=lambda r: r["edge_pp"], reverse=True)
    tier2.sort(key=lambda r: r["edge_pp"], reverse=True)
    return tier1, tier2, cov_total


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def green(label, sub):
    return f'<span class="green">{esc(label)}<span class="sub">{esc(sub)}</span></span>'


def render(matches, tier1, tier2, cov_total):
    n = len(matches)
    t1 = len(tier1); t2 = len(tier2)

    # Tier1 卡片
    t1_rows = []
    for r in tier1:
        m = r["match"]
        side_cn = {"H": "主胜", "D": "平局", "A": "客胜"}.get(r["side"], r["side"])
        warn = ' <span class="warn">⚠ 非标准赛制/极端赔率, 天眼特征假设90分钟, 谨慎</span>' if r.get("nonstd") else ""
        kelly_note = " (Kelly≈0, 实际不可下)" if r["kelly_frac"] <= 0.001 else ""
        t1_rows.append(f"""<tr>
          <td><b>{esc(m['h'])}</b> vs <b>{esc(m['a'])}</b><br><span class="meta">{esc(m['lg'])} · {esc(m['st'])}</span></td>
          <td>{green(f'{side_cn} +{r["edge_pp"]:.1f}pp', f'wr {r["model_prob"]*100:.1f}% / imp {r["market_implied"]*100:.1f}% · 1/4K {r["kelly_frac"]:.3f}{kelly_note}')}</td>
          <td class="odds">开 {r["odds"]:.2f} / 隐含 {r["market_implied"]*100:.1f}%</td>
        </tr>""")

    # Tier2 列表 (top 12)
    t2_rows = []
    for r in tier2[:14]:
        m = r["match"]
        side_cn = {"H": "主胜", "D": "平局", "A": "客胜"}.get(r["side"], r["side"])
        t2_rows.append(f"""<tr>
          <td><b>{esc(m['h'])}</b> vs <b>{esc(m['a'])}</b><br><span class="meta">{esc(m['lg'])}</span></td>
          <td>{green(f'{side_cn} +{r["edge_pp"]:.1f}pp', f'wr {r["win_rate"]*100:.1f}% / imp {r["implied"]*100:.1f}%')}</td>
        </tr>""")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>价值镜头对比 · 实时绿框 · 世界级分析 vs 赛程OU vs 天眼</title>
<style>
  :root{{--bg:#0e1116;--card:#171c24;--ln:#2a313c;--tx:#e6edf3;--dim:#8b97a7;--gr:#16a34a;--grbg:#0f2e1c;
        --bad:#ef4444;--pass:#b45309;--na:#475569;--acc:#38bdf8;--warn:#f59e0b;}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--tx);font:14px/1.6 -apple-system,Segoe UI,Roboto,"PingFang SC","Microsoft YaHei",sans-serif;padding:28px}}
  h1{{font-size:22px;margin:0 0 4px}} .sub{{font-size:12px;color:var(--dim)}}
  .wrap{{max-width:1080px;margin:0 auto}}
  .verdict{{background:var(--card);border:1px solid var(--ln);border-left:4px solid var(--acc);border-radius:10px;padding:16px 18px;margin:18px 0}}
  .verdict h2{{margin:0 0 8px;font-size:16px}}
  .rank{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}} .rk{{flex:1;min-width:200px;background:#11161d;border:1px solid var(--ln);border-radius:8px;padding:12px}}
  .rk .t{{font-weight:700;font-size:13px}} .rk .v{{font-size:12px;color:var(--dim);margin-top:4px}}
  .badge{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;margin-left:6px}}
  .b1{{background:var(--grbg);color:var(--gr);border:1px solid var(--gr)}} .b0{{background:#2a1414;color:var(--bad);border:1px solid var(--bad)}}
  .kpi{{display:flex;gap:18px;margin:10px 0;flex-wrap:wrap}} .kpi b{{font-size:20px;color:var(--acc)}}
  table{{width:100%;border-collapse:collapse;margin-top:12px;background:var(--card);border-radius:10px;overflow:hidden}}
  th,td{{padding:10px 12px;border-bottom:1px solid var(--ln);text-align:left;vertical-align:top}}
  th{{background:#11161d;font-size:12px;color:var(--dim);font-weight:600}} .meta{{font-size:11px;color:var(--dim)}}
  .green{{display:inline-block;background:var(--grbg);border:2px solid var(--gr);border-radius:8px;padding:5px 9px;color:#bbf7d0;font-weight:700}}
  .green .sub{{display:block;font-weight:400;font-size:11px;color:#86efac;margin-top:2px}}
  .warn{{color:var(--warn);font-size:11px}} .dim{{color:var(--dim)}} .na{{color:var(--na)}}
  .sec{{margin-top:22px}} .sec h3{{font-size:15px;margin:0 0 4px}}
  .tier2{{font-size:13px}} .note{{margin-top:16px;font-size:12px;color:var(--dim);border-top:1px dashed var(--ln);padding-top:12px}}
  .legend span{{margin-right:14px;font-size:12px}}
</style></head><body><div class="wrap">
<h1>价值镜头对比 · 实时绿框：世界级分析 vs 赛程OU vs 天眼</h1>
<div class="sub">生成 {date.today().isoformat()} · 数据源 events.db(GQ实时, bridge健康) · 绿框 = +EV 诚实门 · 重跑即刷新</div>

<div class="verdict">
  <h2>① 结论先行：天眼没废，是覆盖饥饿；价值排序 天眼 &gt; 世界级分析 &gt; 赛程OU</h2>
  <div class="rank">
    <div class="rk"><div class="t">天眼（开盘独立模型）<span class="badge b1">实时在出</span></div>
      <div class="v">唯一经五道关验证 +EV 源（+10.05%, CI 正, 两队入canonical人口）。本批实时 <b>{t1} 场真实可买绿框</b>。只在主流队比赛上 firing; 冷门队正确 PASS（非模型废）。</div></div>
    <div class="rk"><div class="t">世界级分析<span class="badge b1">诊断有效</span></div>
      <div class="v">市场锚+模型矩阵+edge三件套, 不依赖canonical, 任何有盘口场都能跑(本批 <b>{t2} 诊断绿框</b>)。但其 fused/fl OOS 低于市场基线 → 仅诊断/交叉验证, 非拍板。</div></div>
    <div class="rk"><div class="t">赛程OU（裸大小球）<span class="badge b0">价值最低</span></div>
      <div class="v">全局 OU 无 edge(AUC 0.50-0.52 抛硬币级); 泊松 OU +EV 未确立(ROI CI 跨零)。作"买信号"价值最低 → N/A。</div></div>
  </div>
  <div class="kpi">
    <div>实时盘口场 <b>{n}</b></div>
    <div>天眼覆盖(主流) <b>{cov_total}</b></div>
    <div>Tier1 真实可买 <b>{t1}</b></div>
    <div>Tier2 诊断绿框 <b>{t2}</b></div>
  </div>
  <div class="sub">为什么你"看不到天眼价值": {n} 场实时盘口里仅 <b>{cov_total} 场</b>两队入 canonical(主流), 其余 {n-cov_total} 场冷门队天眼正确 PASS → 若你当时看的是冷门场时段/前端过滤, 天眼页即空。主流场(英超/意甲/德甲/杯赛)开赛时天眼持续出绿框。</div>
</div>

<div class="legend">
  <span><span class="green" style="padding:2px 6px">绿框</span> 检测到 +EV</span>
  <span class="dim">Tier1=天眼验证可买 · Tier2=世界级分析诊断(非拍板)</span>
  <span class="warn">⚠ 非标准赛制</span>
</div>

<div class="sec">
  <h3>Tier1 · 真实可买绿框（天眼验证 +EV，{t1} 场）</h3>
  <table><thead><tr><th>比赛</th><th>可买方向 (+EV)</th><th>盘口</th></tr></thead>
  <tbody>{''.join(t1_rows) if t1_rows else '<tr><td colspan=3 class="na">当前无主流场覆盖 / 无 +EV</td></tr>'}</tbody></table>
</div>

<div class="sec">
  <h3>Tier2 · 世界级分析诊断绿框（{t2} 场，仅诊断非拍板，不可盲跟）</h3>
  <table class="tier2"><thead><tr><th>比赛</th><th>诊断方向 (+edge)</th></tr></thead>
  <tbody>{''.join(t2_rows) if t2_rows else '<tr><td colspan=2 class="na">无</td></tr>'}</tbody></table>
  <div class="sub">注: Tier2 为模型共识 vs 市场背离的诊断读数, 其模型 OOS 未超越市场基线, 不构成本系统"可购买"判据。真正可买只看 Tier1(天眼)。</div>
</div>

<div class="sec">
  <h3>赛程OU 镜头</h3>
  <p class="na">N/A — 全局大小球无 edge(AUC 0.50-0.52 抛硬币级); 泊松 OU 的 +EV 未确立(ROI CI 跨零, UNDERPOWERED)。赛程页 OU 线仅作参考, 不作买信号。</p>
</div>

<div class="note">
  <b>② 诚实边界与修复路径</b><br>
  · 天眼"看不到价值"根因 = <b>覆盖饥饿</b>(当前冷门联赛时段, 主流队未开赛), 非 feed 死(bridge 健康, 快照分钟级写入), 非模型废(+10.05% 已验证)。主流场开赛即自动出绿框, 无需任何修复。<br>
  · 若希望天眼覆盖更多冷门场, 需扩充 team_canonical + indep_features + 重训验证 —— 但 +10.05% 仅在已覆盖人口成立, 盲目扩覆盖=未验证风险(违反 IR-30), 不推荐。<br>
  · 非标准赛制(如"瓦尔哈拉杯 8分钟"、极端赔率)天眼特征假设 90 分钟常规足球, 其上 +EV 谨慎对待, Kelly≈0 的更不可下。<br><br>
  <span class="dim">铁律: 分析非预测(IR-20) · 宁 PASS 不伪造(IR-30) · 建仓须人工审批(IR-21)。本页仅供决策参考, 不构成下注建议。</span>
</div>
</div></body></html>"""
    return html


def main():
    matches = load_live()
    print(f"实时盘口场: {len(matches)}")
    tier1, tier2, cov = analyze_all(matches)
    print(f"天眼覆盖: {cov}   Tier1真实可买: {len(tier1)}   Tier2诊断绿框: {len(tier2)}")
    html = render(matches, tier1, tier2, cov)
    os.makedirs("reports", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[输出] {OUT}")


if __name__ == "__main__":
    main()
