"""
pipeline/roi_report.py
=======================
P2: 决策闭环 ROI 报表引擎。

输入:
  - bet_records        (1X2 主市场, 已落决策与赛果)
  - submarket_bets     (OU / 平局共识 / 波胆, 已落决策与赛果)
结算:
  按 actual_result / actual_score 判定命中, 计算每注 PnL。
统一注码模型:
  扁平 1 单位/注 (UNIT_STAKE, 默认 ¥100) —— 跨市场可比的 ROI 口径。
  (凯利注码比仍存于原表供参考; ROI 曲线用扁平单位, 避免不同市场 odds 缺失导致不一致)
输出:
  逐场 PnL + 累计本金曲线 + ROI% + 胜率 + 按市场/联赛拆分 + 校准检验。
纯标准库 + sqlite3, 无外部依赖。
"""
from __future__ import annotations
import sqlite3
import re
from typing import Optional, List, Dict, Any

UNIT_STAKE = 100.0  # 每注扁平单位(¥)


# ───────────────────────────────────────────────────────────────────────────
# 工具
# ───────────────────────────────────────────────────────────────────────────
def parse_score(s) -> Optional[tuple]:
    """'2-1' / '2:1' / '2 - 1' → (2, 1); 否则 None。"""
    if not s:
        return None
    m = re.search(r"(\d+)\s*[-:]\s*(\d+)", str(s).strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def total_goals(s) -> Optional[int]:
    p = parse_score(s)
    return (p[0] + p[1]) if p else None


def line_from_sel(sel: str) -> Optional[float]:
    """'under_2.5' → 2.5。"""
    m = re.search(r"([\d.]+)$", str(sel))
    return float(m.group(1)) if m else None


# ───────────────────────────────────────────────────────────────────────────
# 结算
# ───────────────────────────────────────────────────────────────────────────
def settle_1x2(cur) -> List[Dict[str, Any]]:
    """结算 bet_records 中已落盘且含赛果的下注(H/D/A)。"""
    cur.execute(
        "SELECT bet_id, home_team, away_team, league, match_date, predicted_result, "
        "home_odds, draw_odds, away_odds, actual_result, actual_score "
        "FROM bet_records WHERE predicted_result IS NOT NULL AND actual_result IS NOT NULL")
    out = []
    for (bid, h, a, lg, md, pred, ho, do, ao, act, asc_) in cur.fetchall():
        odds_map = {"H": ho, "D": do, "A": ao}
        odds = odds_map.get(pred)
        if not odds or odds <= 1:
            continue
        win = (pred == act)
        pnl = UNIT_STAKE * (odds - 1) if win else -UNIT_STAKE
        out.append({
            "bet_id": bid, "market": "1X2", "selection": pred,
            "home": h, "away": a, "league": lg, "date": md,
            "odds": round(float(odds), 3), "win": win, "pnl": round(pnl, 2),
            "stake": UNIT_STAKE, "actual": act, "score": asc_,
        })
    return out


def settle_submarket(cur) -> List[Dict[str, Any]]:
    """结算 submarket_bets 中 decision='BET' 且含赛果/赔率的记录。"""
    cur.execute(
        "SELECT id, home_team, away_team, league, match_date, market, selection, "
        "best_odds, actual_result, actual_score "
        "FROM submarket_bets WHERE decision='BET'")
    out = []
    for (sid, h, a, lg, md, mkt, sel, odds, act, asc_) in cur.fetchall():
        base = {"bet_id": sid, "market": mkt, "selection": sel, "home": h,
                "away": a, "league": lg, "date": md, "actual": act, "score": asc_}
        if not odds or odds <= 1:
            out.append({**base, "odds": None, "win": None, "pnl": 0.0,
                        "stake": 0.0, "note": "无赔率, 跳过结算"})
            continue
        if not act and not asc_:
            out.append({**base, "odds": round(float(odds), 3), "win": None,
                        "pnl": 0.0, "stake": 0.0, "note": "无赛果, 未结算"})
            continue
        win = None
        if mkt == "OU":
            side = "over" if sel.startswith("over") else ("under" if sel.startswith("under") else None)
            line = line_from_sel(sel)
            tg = total_goals(asc_)
            if side and line is not None and tg is not None:
                win = (tg > line) if side == "over" else (tg < line)
        elif mkt == "DRAW_CONSENSUS":
            win = (act == "D")
        elif mkt == "CS":
            win = (parse_score(sel) == parse_score(asc_))
        if win is None:
            out.append({**base, "odds": round(float(odds), 3), "win": None,
                        "pnl": 0.0, "stake": 0.0, "note": "无法结算(赛果格式/市场类型)"})
            continue
        pnl = UNIT_STAKE * (odds - 1) if win else -UNIT_STAKE
        out.append({**base, "odds": round(float(odds), 3), "win": win,
                    "pnl": round(pnl, 2), "stake": UNIT_STAKE})
    return out


# ───────────────────────────────────────────────────────────────────────────
# 聚合 / 曲线
# ───────────────────────────────────────────────────────────────────────────
def _sort_key(b):
    d = b.get("date") or ""
    return (d if len(d) == 10 else "9999-99-99", b.get("bet_id") or 0)


def compute_roi_curve(settled: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """逐场累计: cum_pnl / cum_staked / roi_pct / bankroll。"""
    rows = sorted([b for b in settled if b.get("stake")], key=_sort_key)
    curve = []
    cum_pnl = 0.0
    cum_staked = 0.0
    for i, b in enumerate(rows, 1):
        cum_pnl += b["pnl"]
        cum_staked += b["stake"]
        roi = (cum_pnl / cum_staked * 100) if cum_staked > 0 else 0.0
        curve.append({
            "idx": i, "date": b.get("date"), "market": b["market"],
            "selection": b["selection"], "stake": b["stake"], "pnl": b["pnl"],
            "cum_pnl": round(cum_pnl, 2), "cum_staked": round(cum_staked, 2),
            "roi_pct": round(roi, 2), "bankroll": round(cum_pnl, 2),
        })
    return curve


def _agg(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    bets = [r for r in records if r.get("stake")]
    n = len(bets)
    staked = sum(r["stake"] for r in bets)
    pnl = sum(r["pnl"] for r in bets)
    wins = sum(1 for r in bets if r.get("win") is True)
    roi = (pnl / staked * 100) if staked > 0 else 0.0
    hit = (wins / n * 100) if n else 0.0
    return {"bets": n, "staked": round(staked, 2), "pnl": round(pnl, 2),
            "roi_pct": round(roi, 2), "wins": wins, "hit_rate": round(hit, 2)}


def build_report(cur) -> Dict[str, Any]:
    s1 = settle_1x2(cur)
    ss = settle_submarket(cur)
    all_settled = s1 + ss
    curve = compute_roi_curve(all_settled)

    # 按市场拆分
    by_market = {}
    for b in all_settled:
        by_market.setdefault(b["market"], []).append(b)
    markets = {m: _agg(rs) for m, rs in by_market.items()}

    # 按联赛拆分 (仅 1X2 + sub 合并, 按 market 细看更有意义; 这里按 league 聚合全部)
    by_league = {}
    for b in all_settled:
        lg = b.get("league") or "未知"
        by_league.setdefault(lg, []).append(b)
    leagues = {lg: _agg(rs) for lg, rs in by_league.items()}

    overall = _agg(all_settled)

    # 待结算机会: 已落 BET 且带赔率, 但赛果尚未回补 (赛果到达后自动进入曲线)
    pending = len([b for b in all_settled if not b.get("stake")])

    # P3 接口位: 实时决策闭环校准(模型自报概率 vs 实际), 懒加载且容错, 不影响 P2 主流程
    calibration_live = None
    try:
        from pipeline.calibration import build_live
        calibration_live = build_live(cur)
    except Exception:
        calibration_live = None

    return {
        "generated_at": _now(),
        "unit_stake": UNIT_STAKE,
        "overall": overall,
        "by_market": markets,
        "by_league": leagues,
        "curve": curve,
        "per_bet": sorted(all_settled, key=_sort_key),
        "unsettled": [b for b in all_settled if not b.get("stake")],
        "pending": pending,
        "calibration_live": calibration_live,
    }


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def print_summary(cur):
    r = build_report(cur)
    o = r["overall"]
    print("\n=== P2 ROI 闭环摘要 ===")
    print(f"可结算下注: {o['bets']} 注 | 总本金 ¥{o['staked']:,.0f} | 净盈亏 ¥{o['pnl']:,.0f} | "
          f"ROI {o['roi_pct']:+.2f}% | 胜率 {o['hit_rate']:.1f}% ({o['wins']}/{o['bets']})")
    print("按市场:")
    for m, a in sorted(r["by_market"].items(), key=lambda x: -x[1]['pnl']):
        print(f"  {m:16s} 注数 {a['bets']:>3} | 本金 ¥{a['staked']:>8,.0f} | "
              f"盈亏 ¥{a['pnl']:>8,.0f} | ROI {a['roi_pct']:+.2f}% | 胜率 {a['hit_rate']:.1f}%")
    uns = len(r["unsettled"])
    if uns:
        print(f"(另有 {uns} 条 BET 已带赔率、待赛果回补后自动结算)")
    return r


# ───────────────────────────────────────────────────────────────────────────
# HTML 报表 (自包含, 内联 SVG 曲线, 无外部依赖)
# ───────────────────────────────────────────────────────────────────────────
def _svg_curve(curve: List[Dict[str, Any]], w=720, h=260) -> str:
    if not curve:
        return "<p style='color:#888'>无可结算数据，无法绘制曲线。</p>"
    xs = [c["idx"] for c in curve]
    ys = [c["roi_pct"] for c in curve]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(min(ys), 0), max(max(ys), 0)
    pad = 36
    def px(x):
        return pad + (x - xmin) / (xmax - xmin or 1) * (w - 2 * pad)
    def py(y):
        return h - pad - (y - ymin) / (ymax - ymin or 1) * (h - 2 * pad)
    # 零线
    zero_y = py(0)
    pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in zip(xs, ys))
    # 网格 + 标签
    grid = ""
    for gx in range(xmin, xmax + 1, max(1, (xmax - xmin) // 6)):
        grid += f"<line x1='{px(gx):.1f}' y1='{pad}' x2='{px(gx):.1f}' y2='{h-pad}' stroke='#222' stroke-width='0.5'/>"
    labels = (f"<text x='{px(xmax):.1f}' y='{py(ys[-1])-6:.1f}' fill='#4ade80' font-size='11' "
              f"text-anchor='end'>ROI {ys[-1]:+.1f}%</text>")
    return f"""<svg viewBox='0 0 {w} {h}' width='100%' style='background:#0d1117;border-radius:8px'>
  <line x1='{pad}' y1='{zero_y:.1f}' x2='{w-pad}' y2='{zero_y:.1f}' stroke='#666' stroke-dasharray='4 3' stroke-width='1'/>
  <text x='{pad}' y='{zero_y-4:.1f}' fill='#888' font-size='10'>0%</text>
  {grid}
  <polyline points='{pts}' fill='none' stroke='#4ade80' stroke-width='2'/>
  {labels}
</svg>"""


def render_html(r: Dict[str, Any]) -> str:
    o = r["overall"]
    mrows = "".join(
        f"<tr><td>{m}</td><td>{a['bets']}</td><td>¥{a['staked']:,.0f}</td>"
        f"<td style='color:{'#4ade80' if a['pnl']>=0 else '#f87171'}'>¥{a['pnl']:,.0f}</td>"
        f"<td>{a['roi_pct']:+.2f}%</td><td>{a['hit_rate']:.1f}%</td></tr>"
        for m, a in sorted(r["by_market"].items(), key=lambda x: -x[1]['pnl']))
    lrows = "".join(
        f"<tr><td>{lg}</td><td>{a['bets']}</td><td>¥{a['staked']:,.0f}</td>"
        f"<td style='color:{'#4ade80' if a['pnl']>=0 else '#f87171'}'>¥{a['pnl']:,.0f}</td>"
        f"<td>{a['roi_pct']:+.2f}%</td><td>{a['hit_rate']:.1f}%</td></tr>"
        for lg, a in sorted(r["by_league"].items(), key=lambda x: -x[1]['pnl']))
    recent = r["per_bet"][-30:]
    brows = "".join(
        f"<tr><td>{b.get('date') or '-'}</td><td>{b['market']}</td><td>{b.get('home')} v {b.get('away')}</td>"
        f"<td>{b['selection']}</td><td>{b.get('odds') or '-'}</td>"
        f"<td>{'✅' if b.get('win') is True else ('❌' if b.get('win') is False else '—')}</td>"
        f"<td style='color:{'#4ade80' if b.get('pnl',0)>=0 else '#f87171'}'>¥{b.get('pnl',0):,.0f}</td></tr>"
        for b in recent)
    return f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<title>ROI 闭环报表</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#010409;color:#c9d1d9;margin:0;padding:24px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:20px}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}}
 .card{{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:14px 18px;min-width:140px}}
 .card .v{{font-size:22px;font-weight:700}} .card .k{{font-size:11px;color:#8b949e}}
 .pos{{color:#4ade80}} .neg{{color:#f87171}}
 table{{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px}}
 th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #21262d}}
 th{{color:#8b949e;font-weight:600}}
 h2{{font-size:15px;margin:24px 0 8px;border-left:3px solid #4ade80;padding-left:8px}}
</style></head><body>
<h1>📊 决策闭环 ROI 报表</h1>
<div class='sub'>生成于 {r['generated_at']} · 注码模型: 扁平 {r['unit_stake']:.0f} 元/注 · 仅统计已结算下注(BET + 含赛果)</div>
<div class='cards'>
  <div class='card'><div class='k'>可结算注数</div><div class='v'>{o['bets']}</div></div>
  <div class='card'><div class='k'>总本金</div><div class='v'>¥{o['staked']:,.0f}</div></div>
  <div class='card'><div class='k'>净盈亏</div><div class='v {'pos' if o['pnl']>=0 else 'neg'}'>¥{o['pnl']:,.0f}</div></div>
  <div class='card'><div class='k'>ROI</div><div class='v {'pos' if o['roi_pct']>=0 else 'neg'}'>{o['roi_pct']:+.2f}%</div></div>
  <div class='card'><div class='k'>胜率</div><div class='v'>{o['hit_rate']:.1f}%</div></div>
  <div class='card'><div class='k'>待结算机会</div><div class='v'>{r.get('pending', 0)}</div></div>
</div>
<h2>累计 ROI 曲线 (逐场)</h2>
{_svg_curve(r['curve'])}
<h2>按市场拆分</h2>
<table><tr><th>市场</th><th>注数</th><th>本金</th><th>净盈亏</th><th>ROI</th><th>胜率</th></tr>{mrows}</table>
<h2>按联赛拆分</h2>
<table><tr><th>联赛</th><th>注数</th><th>本金</th><th>净盈亏</th><th>ROI</th><th>胜率</th></tr>{lrows}</table>
<h2>逐场明细 (最近 30 注)</h2>
<table><tr><th>日期</th><th>市场</th><th>对阵</th><th>选择</th><th>赔率</th><th>命中</th><th>PnL</th></tr>{brows}</table>
<p style='color:#8b949e;font-size:11px'>注: 1X2 主市场依 v6 铁律通常无 edge → 多数 PASS; 真正可量化 edge 在子市场(OU跨市场不一致 / 平局共识跨庄溢价)。本报表为决策闭环验证, 非实盘建议。</p>
</body></html>"""
