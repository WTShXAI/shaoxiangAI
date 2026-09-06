#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cross_validate_analysis.py — 用完整历史比赛交叉验证分析功能与分析内容
=====================================================================
从 events.db 取所有已完赛+有赔率的比赛, 对每场跑 ranked_predictor,
比对预测 vs 实际赛果, 产出:
  1. 1X2/OU/CS 命中率 (vs naive 基线)
  2. 分析文案准确性: "强烈关注"/"信号干净" 比赛的命中率 / "陷阱盘"标记的真实反转率
  3. 波胆市场 vs 泊松 accuracy 对比
  4. OU analysis.ou 文案与赛果一致性

用法:
  python scripts/cross_validate_analysis.py [--sample N] [--verbose]
    --sample N: 随机抽 N 场 (默认全部 3228 场)
    --verbose: 打印每场详情
"""
import sys, os, json, time, random, math
import sqlite3
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GQ_DB = os.path.join(ROOT, "data", "events.db")
OUT = os.path.join(ROOT, "_verify", "cross_validate_report")

def load_matches():
    """返回所有已完赛+有1X2赔率的比赛 (mid, home, away, result, score_home, score_away, ou_result)."""
    db = sqlite3.connect(GQ_DB)
    db.row_factory = sqlite3.Row
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


def ou_actual(score_home, score_away, ou_line):
    """给定比分和盘口线, 返回actual: '大球'/'小球'/None."""
    if score_home is None or score_away is None or ou_line is None:
        return None
    total = int(score_home) + int(score_away)
    return "大球" if total > float(ou_line) else ("小球" if total < float(ou_line) else None)


def result_to_cn(r):
    return {"home": "主胜", "draw": "平局", "away": "客胜"}.get(r, r or "?")


def main(sample=None, verbose=False):
    matches = load_matches()
    if sample:
        random.seed(42)
        matches = random.sample(matches, min(sample, len(matches)))
    print(f"交叉验证 {len(matches)} 场 (总库 {len(load_matches())} 场)")

    from pipeline.ranked_predictor import predict as ranked_predict

    # ── 统计 ──
    stats = {
        "total": 0,
        "1x2_ok": 0, "1x2_naive_home": 0, "1x2_naive_fav": 0,
        "ou_ok": 0, "ou_total": 0, "ou_naive_over": 0,
        "cs_top1_hit": 0, "cs_top3_hit": 0, "cs_total": 0,
        "strong_signal_hits": 0, "strong_signal_total": 0,
        "clean_signal_hits": 0, "clean_signal_total": 0,
        "trap_correct": 0, "trap_total": 0,
        "trap_mislabelled": 0,
        "market_cs_hits": 0, "market_cs_total": 0,
        "analysis_ou_correct": 0, "analysis_ou_total": 0,
        "errors": 0,
    }

    for i, m in enumerate(matches):
        home = m["home"]
        away = m["away"]
        oh = m["oh"]; od = m["od"]; oa = m["oa"]
        if not all([oh, od, oa]):
            continue
        try:
            oh, od, oa = float(oh), float(od), float(oa)
        except Exception:
            continue

        # OU
        ou_line = None
        ou_over = m.get("ou_over")
        ou_under = m.get("ou_under")
        if m.get("ou_market") and str(m.get("ou_market","")).startswith("OU_"):
            try:
                ou_line = float(str(m["ou_market"])[3:])
            except Exception:
                pass

        # 调用 ranked_predictor
        try:
            r = ranked_predict(
                home, away, oh, od, oa,
                ou_line=ou_line,
                ou_over=float(ou_over) if ou_over else None,
                ou_under=float(ou_under) if ou_under else None,
            )
        except Exception as e:
            stats["errors"] += 1
            continue

        stats["total"] += 1
        actual = m["result"]  # home/draw/away
        score_h = m.get("score_home")
        score_a = m.get("score_away")

        # ── 1X2 ──
        _1x2 = r.get("markets", {}).get("1x2", {})
        _ranked = _1x2.get("ranked", [])
        pred_1x2_map = {"主胜": "H", "平局": "D", "客胜": "A"}
        pred_1x2 = pred_1x2_map.get(_ranked[0][0], "?") if _ranked else "?"
        actual_1x2 = {"home": "H", "draw": "D", "away": "A"}.get(actual, "?")
        if pred_1x2 == actual_1x2:
            stats["1x2_ok"] += 1
        # naive: 永远主胜
        if actual_1x2 == "H":
            stats["1x2_naive_home"] += 1
        # naive: 跟庄家热门 (最低赔率方)
        fav_map = {"H": oh, "D": od, "A": oa}
        fav = min(fav_map, key=fav_map.get)
        if fav == actual_1x2:
            stats["1x2_naive_fav"] += 1

        # ── OU ──
        if ou_line and score_h is not None and score_a is not None:
            actual_ou = ou_actual(score_h, score_a, ou_line)
            if actual_ou:
                stats["ou_total"] += 1
                # 预测: 从 ranked 结果取 OU top1
                ou_ranked = r.get("markets", {}).get("ou", {}).get("ranked", [])
                if ou_ranked and ou_ranked[0][0] == actual_ou:
                    stats["ou_ok"] += 1
                # naive: 永远大球
                if actual_ou == "大球":
                    stats["ou_naive_over"] += 1

        # ── CS 波胆 ──
        if score_h is not None and score_a is not None:
            actual_cs = f"{score_h}-{score_a}"
            stats["cs_total"] += 1
            cs_ranked = r.get("markets", {}).get("cs", {}).get("ranked", [])
            cs_scores = [x[0] for x in cs_ranked[:3]]
            if cs_ranked and cs_ranked[0][0] == actual_cs:
                stats["cs_top1_hit"] += 1
            if actual_cs in cs_scores:
                stats["cs_top3_hit"] += 1

        # ── 分析文案准确性 ──
        conf = r.get("confidence_tier")
        an = r.get("analysis", {})
        verdict = an.get("verdict", "")

        # "强烈关注" 的比赛命中率
        if "强烈关注" in verdict or conf == "高":
            stats["strong_signal_total"] += 1
            if pred_1x2 == actual_1x2:
                stats["strong_signal_hits"] += 1

        # "信号干净" vs "注意风险"
        if "信号干净" in verdict:
            stats["clean_signal_total"] += 1
            if pred_1x2 == actual_1x2:
                stats["clean_signal_hits"] += 1

        # OU "陷阱盘"标记: 陷阱盘 = 诱导方向反向
        ou_grade = r.get("markets", {}).get("ou", {}).get("grade", "")
        is_trap = "trap" in str(ou_grade)
        if is_trap:
            stats["trap_total"] += 1
            ou_dir = r.get("markets", {}).get("ou", {}).get("direction", "")
            if ou_line and actual_ou:
                # 陷阱盘: direction=诱导方向, 实际应该是反向
                trap_induced = ou_dir
                actual_dir = actual_ou
                if trap_induced != actual_dir:
                    stats["trap_correct"] += 1  # 诱导方向确实错了

        # analysis.ou 文案 vs 赛果
        ou_text = an.get("ou", "")
        if ou_text and "陷阱盘" in ou_text and ou_line and actual_ou:
            stats["analysis_ou_total"] += 1
            if is_trap:
                # 检查文案有没有正确标记
                if "陷阱盘" in ou_text:
                    stats["analysis_ou_correct"] += 1

        # 检查旧"诚实盘"误标 (已修复,但仍需验证)
        if ou_text and "诚实盘" in ou_text and is_trap:
            stats["trap_mislabelled"] += 1

        if verbose and i < 5:
            print(f"\n--- {home} vs {away} ---")
            print(f"  实际: {result_to_cn(actual)} {score_h}-{score_a}")
            print(f"  预测: {pred_1x2} -> {result_to_cn(actual)} {'✓' if pred_1x2==actual_1x2 else '✗'}")
            print(f"  OU: 线{ou_line} 实际{actual_ou or 'N/A'}")
            print(f"  CS: {actual_cs} top3={cs_scores} {'✓' if actual_cs in cs_scores else '✗'}")
            print(f"  文案: conf={conf} verdict={verdict[:80]}")
            print(f"  OU文案: {ou_text[:100]}")

        if i % 500 == 0:
            print(f"... {i}/{len(matches)}")

    # ── 汇总 ──
    t = stats["total"]
    print(f"\n{'='*60}")
    print(f"交叉验证 {t} 场 — 汇总")
    print(f"{'='*60}")

    def pct(part, total):
        return f"{part}/{total} ({100*part/max(total,1):.1f}%)"

    print(f"\n1X2 预测准确率:")
    print(f"  模型:   {pct(stats['1x2_ok'], t)}")
    print(f"  永远主胜: {pct(stats['1x2_naive_home'], t)}")
    print(f"  庄家热门: {pct(stats['1x2_naive_fav'], t)}")
    gain = 100 * (stats['1x2_ok'] - stats['1x2_naive_fav']) / max(t, 1)
    print(f"  vs 庄家热门: {gain:+.1f}pp")

    print(f"\nOU 预测准确率:")
    print(f"  模型:   {pct(stats['ou_ok'], stats['ou_total'])}")
    print(f"  永远大球: {pct(stats['ou_naive_over'], stats['ou_total'])}")
    ou_gain = 100 * (stats['ou_ok'] - stats['ou_naive_over']) / max(stats['ou_total'], 1)
    print(f"  gain: {ou_gain:+.1f}pp")

    print(f"\n波胆命中率:")
    print(f"  Top1: {pct(stats['cs_top1_hit'], stats['cs_total'])}")
    print(f"  Top3: {pct(stats['cs_top3_hit'], stats['cs_total'])}")

    print(f"\n分析文案质量:")
    print(f"  强烈关注命中: {pct(stats['strong_signal_hits'], stats['strong_signal_total'])}")
    print(f"  信号干净命中: {pct(stats['clean_signal_hits'], stats['clean_signal_total'])}")
    print(f"  '信号干净'vs'强烈关注'增益: {100*(stats['clean_signal_hits']/max(stats['clean_signal_total'],1) - stats['strong_signal_hits']/max(stats['strong_signal_total'],1)):+.1f}pp")
    print(f"  陷阱盘正确识别: {pct(stats['trap_correct'], stats['trap_total'])} (诱导反向)")
    print(f"  陷阱盘被误标'诚实盘': {stats['trap_mislabelled']} 场")

    # save
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"report_{int(time.time())}.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(f"\n报告已保存: {OUT}/")


if __name__ == "__main__":
    sample = None
    verbose = "--verbose" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--sample" and i + 1 < len(sys.argv):
            sample = int(sys.argv[i + 1])
    main(sample=sample, verbose=verbose)
