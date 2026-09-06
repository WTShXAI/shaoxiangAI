"""
classify_real_cases.py — 真实病例批量分类诊断 (实操, 用户已批准理论+100分)

把 classify()(odds_taxonomy) 接到真实病例库(match_outcomes), 对每份赔率报告做
5 维度分类, 输出真实病例群的类别分布。这是"医生上岗后看真实病人"的第一步:
先知道病例群长什么样(多少 obscure / 多少陷阱候选 / 多少内部分歧), 才知道怎么排班。

诚实层(同 diagnostic_probe): is_virtual!=1 / 有完整 1X2 / result 有效 /
LEFT JOIN matches 剔 score_missing=1 假0-0 / kickoff>=cutoff(默认2023-01-01)。

用法:
  python scripts/classify_real_cases.py [--cutoff 2023-01-01] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.odds_taxonomy import OddsCase, classify, DIMENSIONS

# main(主流有DB深诊) 白名单关键词 (可后续按用户口径增删)
MAIN_KW = ("世界杯", "World Cup", "英超", "西甲", "德甲", "意甲", "法甲",
           "中超", "MLS", "英格兰", "欧洲", "巴甲", "阿甲", "欧冠", "英冠")


def _is_main(league: str) -> bool:
    if not league:
        return False
    return any(k in league for k in MAIN_KW)


def _devig_draw(h: float, d: float, a: float) -> float:
    ih, idr, ia = 1 / h, 1 / d, 1 / a
    return idr / (ih + idr + ia)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default="2023-01-01")
    ap.add_argument("--out", default="scripts/classify_real_cases_report.json")
    args = ap.parse_args()

    con = sqlite3.connect(os.path.join(ROOT, "data", "events.db"))
    try:
        rows = con.execute(f"""
            SELECT mo.mid, mo.home, mo.away, mo.league, mo.kickoff,
                   mo.op_1x2_h, mo.op_1x2_d, mo.op_1x2_a,
                   mo.op_ah_line, mo.op_ah_home, mo.op_ah_away,
                   mo.op_ou_line, mo.op_ou_over, mo.op_ou_under,
                   mo.is_virtual
            FROM match_outcomes mo
            LEFT JOIN matches m ON mo.mid = m.mid
            WHERE mo.op_1x2_h IS NOT NULL AND mo.op_1x2_d IS NOT NULL
              AND mo.op_1x2_a IS NOT NULL
              AND mo.is_virtual != 1
              AND mo.result IN ('home','draw','away')
              AND COALESCE(m.score_missing, 0) != 1
        """).fetchall()
    finally:
        con.close()

    per_dim: dict = {d: Counter() for d in DIMENSIONS}
    sig = Counter()  # 完整 5 维签名
    n = 0
    for r in rows:
        (mid, home, away, league, kickoff, h, d, a,
         ah_l, ah_h, ah_a, ou_l, ou_o, ou_u, is_virt) = r
        # 时间切分守卫
        if not kickoff or kickoff < args.cutoff:
            continue
        if not (h > 1.0 and d > 1.0 and a > 1.0):
            continue
        case = OddsCase(
            match_key=str(mid), home=home, away=away, league=league or "",
            home_odds=float(h), draw_odds=float(d), away_odds=float(a),
            ah_line=(float(ah_l) if ah_l is not None else None),
            ah_home_odds=(float(ah_h) if ah_h is not None else None),
            ah_away_odds=(float(ah_a) if ah_a is not None else None),
            ou_line=(float(ou_l) if ou_l is not None else None),
            ou_over_odds=(float(ou_o) if ou_o is not None else None),
            ou_under_odds=(float(ou_u) if ou_u is not None else None),
            is_virtual=bool(is_virt),
            has_db_analysis=_is_main(league or ""),
            stage="opening",  # op_* = 初盘
            draw_alert=_devig_draw(float(h), float(d), float(a)),
        )
        cls = classify(case)
        for dim in DIMENSIONS:
            per_dim[dim][cls[dim]] += 1
        sig[tuple(cls[d] for d in DIMENSIONS)] += 1
        n += 1

    # 输出
    print("=" * 70)
    print(f"真实病例分类诊断 (n={n}, kickoff>={args.cutoff})")
    print("=" * 70)
    for dim in DIMENSIONS:
        print(f"\n[{dim}]")
        for val, cnt in per_dim[dim].most_common():
            print(f"  {val:<22s} {cnt:5d}  {cnt/n:6.2%}")
    print("\n" + "-" * 70)
    print("Top 签名 (5维组合):")
    for s, cnt in sig.most_common(12):
        print(f"  {cnt:5d}  " + " | ".join(f"{d}={v}" for d, v in zip(DIMENSIONS, s)))

    rep = {"n": n, "cutoff": args.cutoff,
           "per_dimension": {d: dict(per_dim[d]) for d in DIMENSIONS},
           "top_signatures": [{"sig": list(s), "count": c}
                               for s, c in sig.most_common(20)]}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(f"\n报告: {args.out}")


if __name__ == "__main__":
    main()
