"""
diagnose_cases.py — 把原先的病例(库内比赛=赔率报告)逐一喂给各读片方法诊断

对每种方法过 diagnostic_probe 真 OOS 考卷, 输出横向对比表 + JSON。
核心判据: 方法方向准确率 是否 > 市场基线(54.85%)。

用法:
  python scripts/diagnose_cases.py [--cutoff 2023-01-01] [--boot 2000] [--limit N] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.diagnostic_probe import DiagnosticProbe
from pipeline.reading_methods import REGISTRY


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2023-01-01")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-strict", action="store_true")
    ap.add_argument("--out", default="scripts/diagnose_cases_report.json")
    args = ap.parse_args()

    probe = DiagnosticProbe()
    rows = []
    for key, cls in REGISTRY.items():
        method = cls()
        rep = probe.run(method, cutoff=args.cutoff, strict_clean=not args.no_strict,
                        limit=args.limit, n_boot=args.boot, seed=42)
        rows.append({
            "method": rep.method_name,
            "n_oos": rep.n_oos,
            "accuracy": rep.accuracy,
            "market_accuracy": rep.market_accuracy,
            "acc_ci_low": rep.accuracy_ci[0],
            "acc_ci_high": rep.accuracy_ci[1],
            "brier": rep.brier,
            "logloss": rep.logloss,
            "roi": rep.roi["roi"],
            "roi_ci_low": rep.roi["ci_low"],
            "roi_ci_high": rep.roi["ci_high"],
        })
        print(f"[{rep.method_name}] acc={rep.accuracy:.2%} (mkt {rep.market_accuracy:.2%}) "
              f"ROI={rep.roi['roi']:+.2%} CI[{rep.roi['ci_low']:+.2%},{rep.roi['ci_high']:+.2%}] "
              f"Brier={rep.brier:.4f} LL={rep.logloss:.4f}")

    base = rows[0]["market_accuracy"]  # market_consensus 即基线
    print("\n" + "=" * 78)
    print(f"诊断结论 (市场基线方向准确率 = {base:.2%})")
    print("=" * 78)
    beats = [r for r in rows if r["method"] != "market_consensus"
             and r["accuracy"] > base + 0.005]
    if beats:
        for r in beats:
            print(f"  ✅ {r['method']}: {r['accuracy']:.2%} 超过基线 (+{(r['accuracy']-base)*100:.2f}pp)")
    else:
        print("  ➖ 无任何方法在真 OOS 下超越市场基线 54.85% (与既有'无 edge'结论自洽)")
        print("     含义: 单张 opening 快照的三个市场(1X2/AH/OU)同源, 重新解算≠提高自身;")
        print("           真杠杆在 opening→live 漂移 / 跨庄分歧 / 独立信息(阵容新闻)。")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"cutoff": args.cutoff, "market_baseline": base,
                   "methods": rows}, f, ensure_ascii=False, indent=2)
    print(f"\n报告已写: {args.out}")


if __name__ == "__main__":
    main()
