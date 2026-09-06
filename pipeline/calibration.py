"""
pipeline/calibration.py
========================
P3: 模型概率校准检验（隐含期望胜率 vs 实际命中率对照）。

两块互补:
  A. 实时决策闭环校准 (Live): 来自 bet_records/submarket_bets 已结算下注,
     检验"预测模型自报概率" 是否与实际命中率一致。样本通常很小 → 明确标注置信不足。
  B. 历史共识校准 (Historical): 来自 odds_features (302,900 行, 多庄家收盘价去抽水
     隐含概率 cimp_* + 实际 outcome)。在 v6 框架下, 我们的"模型概率"= 跨庄共识隐含概率
     ≈ 市场收盘价隐含概率, 故这是对我们 v6 模型概率本身的、大样本校准检验。

纯标准库 + sqlite3, 无外部依赖。输出可靠性图(reliability diagram)、Brier、log-loss、
ECE(期望校准误差)、校准斜率。
"""
from __future__ import annotations
import math
from typing import List, Dict, Any, Tuple, Optional

# 避免循环依赖: 仅从 roi_report 导入结算函数(roi_report 不反向 import 本模块)
from pipeline.roi_report import settle_1x2, settle_submarket


# ───────────────────────────────────────────────────────────────────────────
# 基础统计
# ───────────────────────────────────────────────────────────────────────────
def reliability(points: List[Tuple[float, int]], nbins: int = 10) -> List[Dict[str, Any]]:
    """points = [(predicted_prob, win_0/1), ...] → 等宽分桶的可靠性数据。

    返回每桶: {lo, hi, n, mean_pred, emp_rate, err}。空桶跳过。"""
    if not points:
        return []
    pts = [(min(max(p, 0.0), 1.0), int(w)) for p, w in points]
    buckets: List[Dict[str, Any]] = []
    width = 1.0 / nbins
    for i in range(nbins):
        lo, hi = i * width, (i + 1) * width
        seg = [(p, w) for p, w in pts if lo <= p < hi or (i == nbins - 1 and p == 1.0)]
        if not seg:
            continue
        n = len(seg)
        mean_p = sum(p for p, _ in seg) / n
        emp = sum(w for _, w in seg) / n
        buckets.append({
            "lo": round(lo, 3), "hi": round(hi, 3), "n": n,
            "mean_pred": round(mean_p, 4), "emp_rate": round(emp, 4),
            "err": round(emp - mean_p, 4),
        })
    return buckets


def brier_score(points: List[Tuple[float, int]]) -> float:
    """Brier = mean((p - o)^2), o∈{0,1}。越低越好(0=完美)。"""
    if not points:
        return float("nan")
    return round(sum((min(max(p, 0.0), 1.0) - w) ** 2 for p, w in points) / len(points), 5)


def log_loss(points: List[Tuple[float, int]]) -> float:
    """Log-loss = -mean(o*ln(p) + (1-o)*ln(1-p))。越低越好。"""
    if not points:
        return float("nan")
    s = 0.0
    eps = 1e-15
    for p, w in points:
        p = min(max(p, eps), 1 - eps)
        s += (w * math.log(p) + (1 - w) * math.log(1 - p))
    return round(-s / len(points), 5)


def ece(buckets: List[Dict[str, Any]]) -> float:
    """Expected Calibration Error = Σ (n_i/N) * |emp - mean_pred|。"""
    if not buckets:
        return float("nan")
    total = sum(b["n"] for b in buckets)
    if total == 0:
        return float("nan")
    return round(sum(b["n"] / total * abs(b["err"]) for b in buckets), 5)


def calibration_slope(buckets: List[Dict[str, Any]]) -> Optional[float]:
    """用可靠性点 (mean_pred, emp_rate) 做最小二乘斜率。≈1 → 校准良好。"""
    pts = [(b["mean_pred"], b["emp_rate"]) for b in buckets if b["n"] > 0]
    if len(pts) < 2:
        return None
    n = len(pts)
    sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts); sxy = sum(x * y for x, y in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return round((n * sxy - sx * sy) / denom, 4)


# ───────────────────────────────────────────────────────────────────────────
# A. 实时决策闭环校准
# ───────────────────────────────────────────────────────────────────────────
def fetch_live_points(cur) -> List[Tuple[float, int]]:
    """从 bet_records 取已结算 1X2: 自报概率(home/draw/away_prob 百分比) vs 实际。"""
    cur.execute(
        "SELECT predicted_result, home_prob, draw_prob, away_prob, actual_result "
        "FROM bet_records WHERE predicted_result IS NOT NULL AND actual_result IS NOT NULL")
    pts = []
    for pred, hp, dp, ap, act in cur.fetchall():
        probs = {"H": hp, "D": dp, "A": ap}
        if pred not in probs or probs[pred] is None:
            continue
        p = float(probs[pred]) / 100.0  # 存储为百分比
        if not (0 < p <= 1):
            continue
        pts.append((p, 1 if pred == act else 0))
    # 子市场(若未来有结算): submarket_bets decision='BET' 且含赛果
    cur.execute(
        "SELECT market, selection, model_prob, actual_result, actual_score "
        "FROM submarket_bets WHERE decision='BET' AND (actual_result IS NOT NULL OR actual_score IS NOT NULL)")
    for mkt, sel, mp, act, asc_ in cur.fetchall():
        if mp is None or not (0 < mp <= 1):
            continue
        win = None
        if mkt == "DRAW_CONSENSUS":
            win = 1 if act == "D" else 0
        elif mkt == "OU":
            from pipeline.roi_report import parse_score, total_goals, line_from_sel
            side = "over" if sel.startswith("over") else ("under" if sel.startswith("under") else None)
            line = line_from_sel(sel); tg = total_goals(asc_)
            if side and line is not None and tg is not None:
                win = 1 if (tg > line if side == "over" else tg < line) else 0
        elif mkt == "CS":
            from pipeline.roi_report import parse_score
            win = 1 if parse_score(sel) == parse_score(asc_) else 0
        if win is not None:
            pts.append((float(mp), win))
    return pts


def build_live(cur) -> Dict[str, Any]:
    pts = fetch_live_points(cur)
    buckets = reliability(pts, nbins=10)
    return {
        "section": "live_decision_loop",
        "n": len(pts),
        "brier": brier_score(pts),
        "log_loss": log_loss(pts),
        "ece": ece(buckets),
        "slope": calibration_slope(buckets),
        "buckets": buckets,
        "confidence": "high" if len(pts) >= 200 else ("medium" if len(pts) >= 30 else "low"),
        "note": ("样本充足" if len(pts) >= 200 else
                 "样本偏小, 校准仅作框架验证; 待赛果回补后自动变厚" if len(pts) >= 30 else
                 "样本不足(n<30), 校准结论不可信, 仅验证数据链路"),
    }


# ───────────────────────────────────────────────────────────────────────────
# B. 历史共识校准 (大样本)
# ───────────────────────────────────────────────────────────────────────────
def fetch_historical_points(cur) -> Dict[str, Any]:
    """从 odds_features 取 (cimp_h, cimp_d, cimp_a, outcome)。
    返回 overall(取 argmax 概率) 点 + 每结果点。"""
    cur.execute(
        "SELECT cimp_h, cimp_d, cimp_a, outcome FROM odds_features "
        "WHERE outcome IN ('H','D','A') AND cimp_h+cimp_d+cimp_a BETWEEN 0.99 AND 1.01")
    rows = cur.fetchall()
    overall: List[Tuple[float, int]] = []
    per_outcome: Dict[str, List[Tuple[float, int]]] = {"H": [], "D": [], "A": []}
    for ch, cd, ca, oc in rows:
        probs = {"H": ch, "D": cd, "A": ca}
        best = max(probs, key=probs.get)
        overall.append((probs[best], 1 if best == oc else 0))
        for o in ("H", "D", "A"):
            per_outcome[o].append((probs[o], 1 if oc == o else 0))
    return {"n": len(rows), "overall": overall, "per_outcome": per_outcome}


def build_historical(cur) -> Dict[str, Any]:
    data = fetch_historical_points(cur)
    overall_pts = data["overall"]
    ob = reliability(overall_pts, nbins=10)
    per = {}
    for o in ("H", "D", "A"):
        pb = reliability(data["per_outcome"][o], nbins=10)
        per[o] = {
            "n": len(data["per_outcome"][o]),
            "brier": brier_score(data["per_outcome"][o]),
            "ece": ece(pb),
            "slope": calibration_slope(pb),
            "buckets": pb,
        }
    return {
        "section": "historical_consensus",
        "n": data["n"],
        "brier": brier_score(overall_pts),
        "log_loss": log_loss(overall_pts),
        "ece": ece(ob),
        "slope": calibration_slope(ob),
        "buckets": ob,
        "per_outcome": per,
        "confidence": "high",
        "note": ("多庄家收盘价去抽水隐含概率(cimp_*) 作为 v6 '模型概率' 代理, "
                 "大样本市场效率校准; 非单场预测, 用于检验概率体系整体可靠性"),
    }


# ───────────────────────────────────────────────────────────────────────────
# 汇总
# ───────────────────────────────────────────────────────────────────────────
def build_calibration(cur) -> Dict[str, Any]:
    from datetime import datetime
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "live": build_live(cur),
        "historical": build_historical(cur),
    }


# ───────────────────────────────────────────────────────────────────────────
# 可视化 (内联 SVG, 无外部依赖)
# ───────────────────────────────────────────────────────────────────────────
def svg_reliability(buckets: List[Dict[str, Any]], w: int = 360, h: int = 300,
                    title: str = "可靠性图", color: str = "#4ade80") -> str:
    """y=x 完美校准线 + 各桶 (mean_pred, emp_rate) 散点, 气泡大小∝样本量。"""
    if not buckets:
        return f"<p style='color:#888'>{title}: 无数据</p>"
    pad = 38
    def px(x):
        return pad + x * (w - 2 * pad)
    def py(y):
        return h - pad - y * (h - 2 * pad)
    max_n = max(b["n"] for b in buckets) or 1
    # 对角线
    diag = (f"<line x1='{px(0):.1f}' y1='{py(0):.1f}' x2='{px(1):.1f}' y2='{py(1):.1f}' "
            f"stroke='#666' stroke-dasharray='4 3' stroke-width='1'/>")
    dots = ""
    for b in buckets:
        r = 4 + 10 * (b["n"] / max_n) ** 0.5
        col = "#f87171" if b["err"] < -0.03 else ("#4ade80" if b["err"] > 0.03 else "#facc15")
        dots += (f"<circle cx='{px(b['mean_pred']):.1f}' cy='{py(b['emp_rate']):.1f}' "
                 f"r='{r:.1f}' fill='{col}' fill-opacity='0.75' stroke='#0d1117' stroke-width='0.5'/>")
        dots += (f"<text x='{px(b['mean_pred']):.1f}' y='{py(b['emp_rate'])-r-2:.1f}' "
                 f"fill='#8b949e' font-size='8' text-anchor='middle'>{b['n']}</text>")
    # 轴标签
    ax = (f"<text x='{px(0):.1f}' y='{h-8}' fill='#8b949e' font-size='9'>0</text>"
          f"<text x='{px(1):.1f}' y='{h-8}' fill='#8b949e' font-size='9' text-anchor='end'>1.0</text>"
          f"<text x='{px(0.5):.1f}' y='{h-8}' fill='#8b949e' font-size='9' text-anchor='middle'>预测概率</text>"
          f"<text x='{pad-4:.1f}' y='{py(1):.1f}' fill='#8b949e' font-size='9' text-anchor='end'>1.0</text>"
          f"<text x='{pad-4:.1f}' y='{py(0):.1f}' fill='#8b949e' font-size='9' text-anchor='end'>0</text>"
          f"<text x='{pad+4:.1f}' y='{py(0.5):.1f}' fill='#8b949e' font-size='9' "
          f"text-anchor='start' transform='rotate(-90 {pad+4:.0f} {py(0.5):.0f})'>实际命中率</text>")
    return (f"<div style='font-size:12px;color:#c9d1d9;margin-bottom:4px'>{title}</div>"
            f"<svg viewBox='0 0 {w} {h}' width='100%' style='background:#0d1117;border-radius:8px'>"
            f"{diag}{dots}{ax}</svg>")


def render_html(cal: Dict[str, Any]) -> str:
    lv = cal["live"]; hs = cal["historical"]

    def stat_block(d: Dict[str, Any]) -> str:
        return (f"<div class='card'><div class='k'>样本 n</div><div class='v'>{d['n']:,}</div></div>"
                f"<div class='card'><div class='k'>Brier</div><div class='v'>{d['brier']}</div></div>"
                f"<div class='card'><div class='k'>LogLoss</div><div class='v'>{d['log_loss']}</div></div>"
                f"<div class='card'><div class='k'>ECE</div><div class='v'>{d['ece']}</div></div>"
                f"<div class='card'><div class='k'>校准斜率</div><div class='v'>{d['slope'] if d['slope'] is not None else '—'}</div></div>"
                f"<div class='card'><div class='k'>置信</div><div class='v' style='font-size:15px'>{d['confidence']}</div></div>")

    live_cards = stat_block(lv)
    hist_cards = stat_block(hs)
    hist_diag = svg_reliability(hs["buckets"], title=f"整体(argmax)可靠性 · n={hs['n']:,}")
    per_diags = "".join(
        svg_reliability(hs["per_outcome"][o]["buckets"],
                       title=f"{o} 结果校准 · n={hs['per_outcome'][o]['n']:,}",
                       color="#60a5fa")
        for o in ("H", "D", "A"))

    return f"""<!doctype html><html lang='zh'><head><meta charset='utf-8'>
<title>模型概率校准报告</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#010409;color:#c9d1d9;margin:0;padding:24px}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:20px}}
 .cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}}
 .card{{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px 14px;min-width:90px}}
 .card .v{{font-size:18px;font-weight:700}} .card .k{{font-size:10px;color:#8b949e}}
 .diagrow{{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0 24px}}
 h2{{font-size:15px;margin:24px 0 8px;border-left:3px solid #4ade80;padding-left:8px}}
 .note{{color:#8b949e;font-size:11px;margin:6px 0 16px;line-height:1.5}}
 .flag{{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700}}
 .low{{background:#3b1d1d;color:#f87171}} .med{{background:#3a3416;color:#facc15}} .high{{background:#16331f;color:#4ade80}}
</style></head><body>
<h1>🎯 模型概率校准检验报告 (P3)</h1>
<div class='sub'>生成于 {cal['generated_at']} · 检验"隐含期望胜率 vs 实际命中率" · 纯标准库, 无外部依赖</div>

<h2>A. 实时决策闭环校准 — 预测模型自报概率</h2>
<div class='cards'>{live_cards}</div>
<p class='note'>置信等级: <span class='flag {('low' if lv['confidence']=='low' else 'med' if lv['confidence']=='medium' else 'high')}'>{lv['confidence'].upper()}</span> · {lv['note']}<br>
说明: 取 bet_records 中已结算 1X2 下注, 以模型自报概率(home/draw/away_prob)为预测值, 与实际胜负对照。</p>
<div class='diagrow'>{svg_reliability(lv['buckets'], title=f"Live 可靠性 · n={lv['n']}")}</div>

<h2>B. 历史共识校准 — v6 模型概率(市场隐含)大样本</h2>
<div class='cards'>{hist_cards}</div>
<p class='note'>{hs['note']}<br>
判读: 校准斜率≈1 且 ECE 小 → 概率体系整体可靠(市场有效); 斜率明显<1 → 过度自信; 斜率>1 → 过度保守。
气泡大小=桶内样本量, 红=实际低于预测(过度自信), 绿=实际高于预测(保守), 黄=贴合。</p>
<div class='diagrow'>{hist_diag}</div>
<h2>分结果校准 (H / D / A 各自边缘概率)</h2>
<div class='diagrow'>{per_diags}</div>

<p style='color:#8b949e;font-size:11px;margin-top:24px'>
注: 在 FootballAI v6 框架下, 1X2 主市场无超越赔率的信息优势, 故"模型概率"= 跨庄共识隐含概率 ≈ 市场收盘价隐含概率。
本校准检验该概率体系是否整体可靠(市场效率), 非单场胜负预测。Live 闭环(n={lv['n']})将在赛果回补后自动增厚。
</p>
</body></html>"""
