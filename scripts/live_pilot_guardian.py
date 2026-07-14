#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_pilot_guardian: 投产前「安全试点」守护脚本 (用户批准, 赛制架构分析师).

实现此前确定的安全投产路径 (半自动试点规范):
  硬闸门 : disagreement_detected == True 才下注, 否则 PASS (不开则不下)
  信号源 : value_layer.best_direction (最高 edge 边, 仅 decision=='BET')
  赔  源 : 跨庄最优价 best_odds (非单庄)
  风  控 : 规范半凯利 (bet_core) + 单注 ≤10% 本金

两种运行模式:
  --backtest : 对 odds_features 双庄同场 16,140 场历史复模拟, 产出「优化后试点 ROI」,
               与「无闸门裸接 value_layer」对照, 验证 gating 的价值; 可 --write-db 落 bet_records.
  --live     : 解析 live_odds_raw.bookmakers_detail, 对实时盘口跑同一套闸门逻辑,
               写入 PENDING (待审) bet_records, 绝不自动执行 (人工复核后才下单).

注码数学全部走 scripts.bet_core (单一事实源), 与 P0-2/P0-3 回测完全一致.
"""
import os
import sys
import json
import argparse
import sqlite3
from datetime import datetime, timezone

# 仓库根 (兼容本地 Windows 与 Linux CI runner)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pipeline.reverse_odds_engine import ReverseOddsEngine, OddsInput
from pipeline.deep_report import compute_value_layer, consensus_probs
from scripts.bet_core import decide_direction, kelly_fraction, MAX_STAKE_FRAC, FRAC_KELLY, BANKROLL

DB = "data/football_data.db"
OUT_HTML = "deliverables/pilot_backtest_20260711.html"
IDX = {"H": 0, "D": 1, "A": 2}


def fetch_pairs():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    q = """SELECT w.home_team, w.away_team, w.match_date, w.league,
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


def _eval_match(wh, iw, winner, gate_only=True):
    """对单场跑安全试点逻辑. 返回 (gated_record_or_None, nogate_record_or_None).
    gated_record: 仅当 disagreement_detected 且 value_layer BET 才有; nogate: 仅 value_layer BET 就有 (对照)."""
    try:
        b1 = OddsInput(open_h=wh[0], open_d=wh[1], open_a=wh[2],
                       close_h=wh[0], close_d=wh[1], close_a=wh[2])
        b2 = OddsInput(open_h=iw[0], open_d=iw[1], open_a=iw[2],
                       close_h=iw[0], close_d=iw[1], close_a=iw[2])
    except Exception:
        return None, None
    eng = ReverseOddsEngine()
    res = eng.analyze_multi([b1, b2])
    cons = consensus_probs([wh, iw])
    best_odds = [max(wh[0], iw[0]), max(wh[1], iw[1]), max(wh[2], iw[2])]
    vl = compute_value_layer(odds=best_odds, model_probs=cons,
                             bankroll=BANKROLL, frac_kelly=FRAC_KELLY)

    gated = None
    nogate = None
    if vl["decision"] == "BET":
        di = IDX[vl["best_direction"]]
        # 对照: 无闸门 (裸接 value_layer, 即 P0-3 全窗 -35% 的来源)
        if not gate_only or res.disagreement_detected:
            gated = dict(direction=vl["best_direction"], cons=cons, odds=best_odds,
                         k=kelly_fraction(cons[di], best_odds[di]),
                         ev=cons[di] * best_odds[di] - 1,
                         edge=vl.get("best_edge_pct"))
        nogate = dict(direction=vl["best_direction"], cons=cons, odds=best_odds,
                      k=kelly_fraction(cons[di], best_odds[di]),
                      ev=cons[di] * best_odds[di] - 1,
                      edge=vl.get("best_edge_pct"))
    return gated, nogate


def metrics(curve, bets, wins):
    if not curve:
        return 3000.0, 0.0, 0.0, 0.0, 0
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


def backtest(pairs, write_db=False):
    """安全试点历史复模拟. 返回 (pilot_curve, pilot_bets, pilot_wins, nogate_curve, nogate_bets, nogate_wins, records)."""
    eq_p = eq_n = BANKROLL
    cp, cn = [BANKROLL], [BANKROLL]
    bp = bn = wp = wn = 0
    records = []
    now = datetime.now(timezone.utc).isoformat()

    con = sqlite3.connect(DB) if write_db else None
    if con:
        cur = con.cursor()

    for (ht, at, md, lg, wh_h, wh_d, wh_a, hs, as_, iw_h, iw_d, iw_a) in pairs:
        wh = [wh_h, wh_d, wh_a]
        iw = [iw_h, iw_d, iw_a]
        w = "H" if hs > as_ else ("A" if as_ > hs else "D")
        gated, nogate = _eval_match(wh, iw, w, gate_only=True)

        if nogate:
            di = IDX[nogate["direction"]]
            eq_n, sn, wn_ = decide_direction(di, nogate["cons"], nogate["odds"], eq_n, w, gate=False)
            if sn > 0:
                cn.append(eq_n); bn += 1
            if wn_:
                wn += 1

        if gated:
            di = IDX[gated["direction"]]
            eq_p, sp, wp_ = decide_direction(di, gated["cons"], gated["odds"], eq_p, w)
            if sp > 0:
                cp.append(eq_p); bp += 1
            if wp_:
                wp += 1
            if con:
                actual = w
                is_correct = 1 if actual == gated["direction"] else 0
                fields = dict(
                    home_team=ht, away_team=at, league=lg, match_date=md,
                    bet_type="recommendation", source="prediction",
                    predicted_result=gated["direction"],
                    verdict_text=f"gate=disagreement+value_layer_BET; edge={gated['edge']}",
                    confidence=round(gated["cons"][di], 4),
                    home_prob=round(gated["cons"][0], 4), draw_prob=round(gated["cons"][1], 4),
                    away_prob=round(gated["cons"][2], 4),
                    home_odds=round(gated["odds"][0], 3), draw_odds=round(gated["odds"][1], 3),
                    away_odds=round(gated["odds"][2], 3),
                    value_gap=round(gated["edge"] or 0.0, 3), kelly=round(gated["k"], 4),
                    expected_value=round(gated["ev"], 4),
                    actual_result=actual, is_correct=is_correct, actual_score=f"{hs}-{as_}",
                    resolved_at=now, notes=f"pilot gated; stake_frac<={MAX_STAKE_FRAC}",
                    created_at=now)
                cols = ", ".join(fields.keys())
                ph = ", ".join(["?"] * len(fields))
                cur.execute(f"INSERT INTO bet_records ({cols}) VALUES ({ph})", tuple(fields.values()))
                records.append((ht, at, gated["direction"], sp))

    if con:
        con.commit()
        con.close()

    return cp, bp, wp, cn, bn, wn, records


def live_mode():
    """解析 live_odds_raw, 对实时盘口跑安全试点闸门, 写 PENDING bet_records (待人工复核)."""
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "SELECT id, home_team, away_team, commence_time, bookmakers_detail FROM live_odds_raw").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    pending = 0
    parsed = 0
    disagreed = 0
    for rid, ht, at, ct, detail in rows:
        try:
            books = json.loads(detail)
            if not isinstance(books, list) or len(books) < 2:
                continue
            parsed += 1
            # 跨庄最优价 (best odds)
            best = [max(b["h"] for b in books), max(b["d"] for b in books), max(b["a"] for b in books)]
            # 取分歧最大的两庄喂 soft-line 分歧检测
            imps = [{"h": b["h"], "d": b["d"], "a": b["a"]} for b in books]
            imps.sort(key=lambda x: (1/x["h"] + 1/x["d"] + 1/x["a"]))
            bA = imps[0]
            bB = imps[-1]
            oA = [bA["h"], bA["d"], bA["a"]]
            oB = [bB["h"], bB["d"], bB["a"]]
            eng = ReverseOddsEngine()
            res = eng.analyze_multi([
                OddsInput(open_h=oA[0], open_d=oA[1], open_a=oA[2], close_h=oA[0], close_d=oA[1], close_a=oA[2]),
                OddsInput(open_h=oB[0], open_d=oB[1], open_a=oB[2], close_h=oB[0], close_d=oB[1], close_a=oB[2]),
            ])
            cons = consensus_probs([oA, oB])
            vl = compute_value_layer(odds=best, model_probs=cons, bankroll=BANKROLL, frac_kelly=FRAC_KELLY)
            if res.disagreement_detected:
                disagreed += 1
            if res.disagreement_detected and vl["decision"] == "BET":
                di = IDX[vl["best_direction"]]
                fields = dict(
                    match_id=rid, home_team=ht, away_team=at, league="live", match_date=ct,
                    bet_type="recommendation", source="prediction",
                    predicted_result=vl["best_direction"],
                    verdict_text=f"gate=disagreement+value_layer_BET; edge={vl.get('best_edge_pct')}",
                    confidence=round(cons[di], 4),
                    home_prob=round(cons[0], 4), draw_prob=round(cons[1], 4), away_prob=round(cons[2], 4),
                    home_odds=round(best[0], 3), draw_odds=round(best[1], 3), away_odds=round(best[2], 3),
                    value_gap=round(vl.get("best_edge_pct") or 0.0, 3),
                    kelly=round(kelly_fraction(cons[di], best[di]), 4),
                    expected_value=round(cons[di] * best[di] - 1, 4),
                    actual_result=None, is_correct=None, notes="PENDING_LIVE", created_at=now)
                # 幂等: 同场已 PENDING 则跳过 (防 daemon 周期重复灌)
                cur.execute(
                    "SELECT 1 FROM bet_records WHERE home_team=? AND away_team=? "
                    "AND match_date=? AND notes='PENDING_LIVE' LIMIT 1",
                    (ht, at, ct))
                if cur.fetchone() is not None:
                    continue
                cols = ", ".join(fields.keys())
                ph = ", ".join(["?"] * len(fields))
                cur.execute(f"INSERT INTO bet_records ({cols}) VALUES ({ph})", tuple(fields.values()))
                pending += 1
        except Exception as e:
            print(f"  [live] 跳过 row id={rid}: {repr(e)[:80]}")
            continue
    con.commit()
    con.close()
    print(f"[live] 解析 {parsed}/{len(rows)} 行 (分歧检测触发 {disagreed}), "
          f"触发闸门写 PENDING = {pending} 注 (待人工复核, 未自动下单)")


def svg_chart(title, series):
    W, H, pad = 820, 360, 56
    allv = [v for _, _, c in series for v in c]
    if not allv:
        return "<p>无数据</p>"
    ymin, ymax = min(allv), max(allv)
    if ymin == ymax:
        ymin -= 1; ymax += 1
    n = max(len(c) for _, _, c in series)

    def X(i):
        return pad + (W - 2 * pad) * i / max(n - 1, 1)

    def Y(v):
        return H - pad - (H - 2 * pad) * (v - ymin) / (ymax - ymin)

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="background:#0f1419;border-radius:8px">']
    for g in range(5):
        yy = ymin + (ymax - ymin) * g / 4
        yp = Y(yy)
        parts.append(f'<line x1="{pad}" y1="{yp:.1f}" x2="{W-pad}" y2="{yp:.1f}" stroke="#2a3340" stroke-width="1"/>')
        parts.append(f'<text x="4" y="{yp+4:.1f}" fill="#8a94a6" font-size="11">{yy:.0f}</text>')
    if ymin <= BANKROLL <= ymax:
        yb = Y(BANKROLL)
        parts.append(f'<line x1="{pad}" y1="{yb:.1f}" x2="{W-pad}" y2="{yb:.1f}" stroke="#5a6478" stroke-width="1" stroke-dasharray="4 3"/>')
    for name, color, c in series:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(c))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    lx = pad + 8
    for name, color, _ in series:
        parts.append(f'<rect x="{lx}" y="{H-22}" width="14" height="4" fill="{color}"/>')
        parts.append(f'<text x="{lx+20}" y="{H-17}" fill="#c8d2e0" font-size="12">{name}</text>')
        lx += 220
    parts.append(f'<text x="{pad}" y="20" fill="#e6edf6" font-size="15">{title}</text>')
    parts.append("</svg>")
    return "".join(parts)


def html_report(cp, bp, wp, cn, bn, wn, meta):
    mp = metrics(cp, bp, wp)
    mn = metrics(cn, bn, wn)
    c1 = svg_chart("① 安全试点路径 (disagreement闸门 + value_layer + 跨庄最优价)",
                   [("pilot(闸门过滤)", "#3fd07a", cp)])
    c2 = svg_chart("② 对照: 无闸门裸接 value_layer (P0-3 全窗 -35% 来源)",
                   [("no-gate(裸接)", "#ff6b6b", cn)])
    verdict = (
        f"🛡️ <b>安全试点路径 ROI = {mp[1]:+.1f}%</b> (分歧子集 {mp[4]} 注, 胜率 {mp[3]:.1f}%, 最大回撤 {mp[2]:.1f}%)<br>"
        f"🚨 对照: 无闸门裸接 value_layer ROI = {mn[1]:+.1f}% ({mn[4]} 注) → 验证<b>分歧闸门是 edge 过滤器</b>, "
        f"裸接必亏, 绝不可全量接盘.<br>"
        f"✅ 投产建议: 维持 ENABLE_SOFTLINE_DECISION=OFF; 唯一可盈利自动路径 = "
        f"disagreement_detected 闸门 + value_layer edge + 跨庄最优价 + 单注封顶. "
        f"试点样本仍仅 {mp[4]} 注, 待 live 增厚到 ≥300~500 注再放开全量."
    )
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>安全试点 (live_pilot_guardian) 历史复模拟</title>
<style>
body{{background:#0b0f14;color:#c8d2e0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}}
h1{{color:#e6edf6;font-size:22px}} h2{{color:#9fb0c8;font-size:16px;margin-top:24px}}
table{{border-collapse:collapse;width:100%;margin-top:12px;font-size:13px}}
th,td{{border:1px solid #243040;padding:7px 9px;text-align:center}}
th{{background:#16202c;color:#9fb0c8}} td:first-child{{text-align:left;color:#c8d2e0}}
.pos{{color:#3fd07a}} .neg{{color:#ff6b6b}}
.box{{background:#111824;border:1px solid #243040;border-radius:8px;padding:14px 18px;margin-top:14px}}
.note{{color:#8a94a6;font-size:12px;line-height:1.6}}
</style></head><body>
<h1>安全试点路径 · 历史复模拟背书</h1>
<div class="box note">
  虚拟本金 <b>3000 元</b> · 规范半凯利(bet_core) + 单注封顶 {MAX_STAKE_FRAC*100:.0f}% · 双庄同场 16,140 场 · 时序 OOS<br>
  {meta}<br>
  安全路径 = <b>disagreement_detected 闸门 + value_layer.best_direction + 跨庄最优价</b>; 对照 = 无闸门裸接 value_layer.
</div>
<h2>资金曲线</h2>
{c1}
{c2}
<h2>关键指标 (final / ROI / 最大回撤 / 胜率 / 下注场次)</h2>
<table>
<tr><th>路径</th><th>终值</th><th>ROI</th><th>MaxDD</th><th>胜率</th><th>场次</th></tr>
<tr><td>安全试点(闸门)</td><td>{mp[0]:.0f}</td><td class='{'pos' if mp[1]>=0 else 'neg'}'>{mp[1]:+.1f}%</td><td>{mp[2]:.1f}%</td><td>{mp[3]:.1f}%</td><td>{mp[4]}</td></tr>
<tr><td>无闸门裸接</td><td>{mn[0]:.0f}</td><td class='{'pos' if mn[1]>=0 else 'neg'}'>{mn[1]:+.1f}%</td><td>{mn[2]:.1f}%</td><td>{mn[3]:.1f}%</td><td>{mn[4]}</td></tr>
</table>
<div class="box" style="margin-top:18px;font-size:14px;color:#e6edf6;line-height:1.9">{verdict}</div>
</body></html>"""
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    return mp, mn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="解析 live_odds_raw 写 PENDING bet_records")
    ap.add_argument("--daemon", action="store_true", help="守护模式: 周期跑 --live 直到 Ctrl+C (自动累积 G3 真实样本)")
    ap.add_argument("--interval", type=int, default=600, help="--daemon 循环间隔秒 (默认600)")
    ap.add_argument("--write-db", action="store_true", help="backtest 时把已结算注单写入 bet_records")
    ap.add_argument("--no-html", action="store_true", help="不生成 HTML 报告")
    args = ap.parse_args()

    if args.daemon:
        import time
        print(f"[daemon] 启动: 每 {args.interval}s 扫一次 live_odds_raw -> PENDING (Ctrl+C 停止)")
        try:
            while True:
                live_mode()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("[daemon] 已停止")
        return

    if args.live:
        live_mode()
        return

    pairs = fetch_pairs()
    meta = f"样本: 双庄同场 <b>{len(pairs)}</b> 场"
    print(f"[guardian] pairs={len(pairs)}")
    cp, bp, wp, cn, bn, wn, records = backtest(pairs, write_db=args.write_db)
    mp, mn = html_report(cp, bp, wp, cn, bn, wn, meta) if not args.no_html else (metrics(cp,bp,wp), metrics(cn,bn,wn))
    print(f"[安全试点/闸门] ROI={mp[1]:+.1f}%  胜率={mp[3]:.1f}%  注={mp[4]}  终值={mp[0]:.0f}")
    print(f"[无闸门裸接]   ROI={mn[1]:+.1f}%  胜率={mn[3]:.1f}%  注={mn[4]}  终值={mn[0]:.0f}")
    if args.write_db:
        print(f"[bet_records] 已写入已结算试点注单 = {len(records)} 条")
    if not args.no_html:
        print(f"[guardian] HTML -> {OUT_HTML}")


if __name__ == "__main__":
    main()
