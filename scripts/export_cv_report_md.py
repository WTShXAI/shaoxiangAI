#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
export_cv_report_md.py — 导出完整交叉验证报告 (Markdown)
==========================================================
输出: 2000场逐场预测 vs 赛果 + 汇总统计 + 校准分析
"""
import sys, os, json, time, random, math, datetime
import sqlite3
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GQ_DB = os.path.join(ROOT, "data", "events.db")
OUT_DIR = os.path.join(ROOT, "_verify", "cross_validate_report")
os.makedirs(OUT_DIR, exist_ok=True)
OUT = os.path.join(OUT_DIR, "full_report_2000.md")


def load_matches():
    db = sqlite3.connect(GQ_DB); db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT mo.mid, mo.home, mo.away, mo.result, mo.score_home, mo.score_away,
          (SELECT odds FROM odds_snapshots WHERE match_key=mo.home||' vs '||mo.away AND market='1X2' AND selection='home' ORDER BY captured_at DESC LIMIT 1) as oh,
          (SELECT odds FROM odds_snapshots WHERE match_key=mo.home||' vs '||mo.away AND market='1X2' AND selection='draw' ORDER BY captured_at DESC LIMIT 1) as od,
          (SELECT odds FROM odds_snapshots WHERE match_key=mo.home||' vs '||mo.away AND market='1X2' AND selection='away' ORDER BY captured_at DESC LIMIT 1) as oa,
          (SELECT market FROM odds_snapshots WHERE match_key=mo.home||' vs '||mo.away AND market LIKE 'OU_%' AND market NOT LIKE '%1H%' AND market NOT LIKE '%2H%' ORDER BY captured_at DESC LIMIT 1) as ou_market,
          (SELECT odds FROM odds_snapshots WHERE match_key=mo.home||' vs '||mo.away AND market LIKE 'OU_%' AND market NOT LIKE '%1H%' AND market NOT LIKE '%2H%' AND selection='over' ORDER BY captured_at DESC LIMIT 1) as ou_over,
          (SELECT odds FROM odds_snapshots WHERE match_key=mo.home||' vs '||mo.away AND market LIKE 'OU_%' AND market NOT LIKE '%1H%' AND market NOT LIKE '%2H%' AND selection='under' ORDER BY captured_at DESC LIMIT 1) as ou_under
        FROM match_outcomes mo
        WHERE mo.result IS NOT NULL
          AND EXISTS(SELECT 1 FROM odds_snapshots WHERE match_key=mo.home||' vs '||mo.away AND market='1X2')
        ORDER BY mo.mid
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def ou_actual(sh, sa, line):
    if sh is None or sa is None or line is None: return None
    total = int(sh) + int(sa)
    return "大球" if total > float(line) else ("小球" if total < float(line) else None)


def fmt_pct(n, d):
    return f"{n}/{d} ({100*n/max(d,1):.1f}%)"


def main():
    matches = load_matches()
    random.seed(42)
    matches = random.sample(matches, min(2000, len(matches)))
    print(f"导出 {len(matches)} 场交叉验证到 {OUT} ...")

    from pipeline.ranked_predictor import predict as ranked_predict

    records = []
    stats = defaultdict(int)

    for i, m in enumerate(matches):
        home = m["home"]; away = m["away"]
        oh = m["oh"]; od = m["od"]; oa = m["oa"]
        if not all([oh, od, oa]): continue
        try: oh, od, oa = float(oh), float(od), float(oa)
        except: continue

        ou_line = None
        if m.get("ou_market") and str(m.get("ou_market","")).startswith("OU_"):
            try: ou_line = float(str(m["ou_market"])[3:])
            except: pass

        ou_over = m.get("ou_over"); ou_under = m.get("ou_under")
        try:
            r = ranked_predict(home, away, oh, od, oa,
                ou_line=ou_line,
                ou_over=float(ou_over) if ou_over else None,
                ou_under=float(ou_under) if ou_under else None)
        except: continue

        actual = m["result"]
        score_h = m.get("score_home"); score_a = m.get("score_away")
        actual_1x2_cn = {"home":"主胜","draw":"平局","away":"客胜"}.get(actual,"?")
        actual_1x2 = {"home":"H","draw":"D","away":"A"}.get(actual,"?")

        _1x2 = r.get("markets",{}).get("1x2",{})
        _ranked_1x2 = _1x2.get("ranked",[])
        pred_1x2 = {"主胜":"H","平局":"D","客胜":"A"}.get(_ranked_1x2[0][0],"?" ) if _ranked_1x2 else "?"
        pred_cn = {"H":"主胜","D":"平局","A":"客胜"}.get(pred_1x2,"?")

        is_correct = "✅" if pred_1x2 == actual_1x2 else "❌"
        fav = min({"H":oh,"D":od,"A":oa}, key=lambda k: {"H":oh,"D":od,"A":oa}[k])
        fav_ok = "✅" if fav == actual_1x2 else "❌"

        stats["total"] += 1
        if pred_1x2 == actual_1x2: stats["1x2_ok"] += 1
        if fav == actual_1x2: stats["1x2_naive_fav"] += 1

        # OU
        ou_actual_val = ou_actual(score_h, score_a, ou_line)
        ou_pred = ""
        _ou = r.get("markets",{}).get("ou",{})
        _ou_ranked = _ou.get("ranked",[])
        if _ou_ranked and ou_actual_val:
            ou_pred = _ou_ranked[0][0]
            stats["ou_total"] += 1
            if _ou_ranked[0][0] == ou_actual_val: stats["ou_ok"] += 1

        # CS
        actual_cs = f"{score_h}-{score_a}" if score_h is not None else ""
        _cs = r.get("markets",{}).get("cs",{}).get("ranked",[])
        cs_pred = _cs[0][0] if _cs else ""
        cs_top3_hit = actual_cs in [x[0] for x in _cs[:3]] if _cs else False
        if actual_cs:
            stats["cs_total"] += 1
            if cs_pred == actual_cs: stats["cs_top1_hit"] += 1
            if cs_top3_hit: stats["cs_top3_hit"] += 1

        # Confidence
        conf = r.get("confidence_tier","")
        verdict = r.get("analysis",{}).get("verdict","")
        is_strong = "强烈关注" in verdict or conf == "高"
        ou_grade = _ou.get("grade","")
        is_trap = "trap" in str(ou_grade)
        ou_dir = _ou.get("direction","")

        if is_strong:
            stats["strong_total"] += 1
            if pred_1x2 == actual_1x2: stats["strong_hits"] += 1
        if is_trap:
            stats["trap_total"] += 1
            if ou_dir != ou_actual_val:
                stats["trap_correct"] += 1

        records.append({
            "home": home, "away": away,
            "oh": round(oh,2), "od": round(od,2), "oa": round(oa,2),
            "ou_line": ou_line or "",
            "actual": actual_1x2_cn,
            "actual_score": f"{score_h}-{score_a}" if score_h is not None else "",
            "actual_ou": ou_actual_val or "",
            "pred_1x2": pred_cn,
            "pred_correct": is_correct,
            "fav_baseline": fav_ok,
            "pred_prob": round(_ranked_1x2[0][1] if _ranked_1x2 else 0, 3),
            "pred_ou": f"{_ou_ranked[0][0]} {_ou_ranked[0][1]:.1%}" if _ou_ranked else "",
            "pred_cs": cs_pred,
            "cs_top3_hit": "✓" if cs_top3_hit else "",
            "conf_tier": conf,
            "verdict": verdict[:80],
            "is_trap": "陷阱" if is_trap else "",
        })

        if i % 500 == 0: print(f"... {i}")

    # ── 写 Markdown ──
    lines = []
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    t = stats["total"]
    lines.append(f"# 哨响AI 交叉验证报告")
    lines.append(f"**生成时间**: {now_str}  |  **样本**: {t} 场 (随机种子 42)  |  **后端**: v7.4")
    lines.append("")

    lines.append("## 汇总指标")
    lines.append("| 指标 | 值 |")
    lines.append("|------|-----|")
    lines.append(f"| 1X2 模型 | {fmt_pct(stats['1x2_ok'], t)} |")
    lines.append(f"| 庄家热门基线 | {fmt_pct(stats['1x2_naive_fav'], t)} |")
    gain = 100 * (stats['1x2_ok'] - stats['1x2_naive_fav']) / max(t, 1)
    lines.append(f"| vs 庄家热门 | {gain:+.1f}pp |")
    lines.append(f"| OU 模型 | {fmt_pct(stats['ou_ok'], stats['ou_total'])} |")
    lines.append(f"| 波胆 Top1 | {fmt_pct(stats['cs_top1_hit'], stats['cs_total'])} |")
    lines.append(f"| 波胆 Top3 | {fmt_pct(stats['cs_top3_hit'], stats['cs_total'])} |")
    lines.append(f"| 强烈关注命中 | {fmt_pct(stats['strong_hits'], stats['strong_total'])} |")
    if stats['trap_total'] > 0:
        lines.append(f"| 陷阱盘正确识别 | {fmt_pct(stats['trap_correct'], stats['trap_total'])} |")
    lines.append(f"| 预测失败 | {t - stats['1x2_ok']} 场 |")
    lines.append("")

    # ── 逐场明细 ──
    lines.append(f"## 逐场明细 ({t} 场)")
    lines.append("")
    lines.append("| # | 主队 | 客队 | 1X2赔率 | 实际 | 比分 | 预测1X2 | 预测OU | 波胆 | Top3 | 把握度 |")
    lines.append("|---|------|------|---------|------|------|---------|--------|------|------|--------|")

    for i, rec in enumerate(records):
        ou_label = f"线{rec['ou_line']}" if rec['ou_line'] else ""
        lines.append(f"| {i+1} | {rec['home'][:12]} | {rec['away'][:12]} | {rec['oh']}/{rec['od']}/{rec['oa']} | {rec['actual']} {rec['actual_score']} | {rec['pred_1x2']} {rec['pred_correct']} | {rec['pred_ou']} | {rec['pred_cs']} | {rec['cs_top3_hit']} | {rec['conf_tier']} {rec['is_trap']} |")

    # ── 错误分析 ──
    errors = [r for r in records if "❌" in r["pred_correct"]]
    lines.append("")
    lines.append(f"## 预测错误分析 ({len(errors)} 场)")
    lines.append("| 主队 | 客队 | 赔率 | 实际 | 预测 | 把握度 | 陷阱 |")
    lines.append("|------|------|------|------|------|--------|------|")
    for r in errors:
        lines.append(f"| {r['home'][:12]} | {r['away'][:12]} | {r['oh']}/{r['od']}/{r['oa']} | {r['actual']} | {r['pred_1x2']} | {r['conf_tier']} | {r['is_trap']} |")

    # ── 信心校准 ──
    lines.append("")
    lines.append("## 信心校准")
    lines.append("分析: 强烈关注比赛 (N={}) 的实际命中率为 {}。".format(stats['strong_total'], fmt_pct(stats['strong_hits'], stats['strong_total'])))

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n✅ 已导出: {OUT}  ({len(lines)} 行)")
    print(f"1X2={fmt_pct(stats['1x2_ok'],t)} vs fav={fmt_pct(stats['1x2_naive_fav'],t)}")


if __name__ == "__main__":
    main()
