"""demo_multibook.py — 多庄 sharp 共识引擎验证 + 方向建议演示

读 leisu_odds 1X2 分组, 跑 sharp 锚定共识 + retail 背离 + 方向 edge,
输出每场的价值侧/fade侧方向建议, 并演示如何作为 unified_predictor 的 market prior。

用法: python scripts/demo_multibook.py [--include-mock]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

# 让脚本可直接 `python scripts/demo_multibook.py` 运行, 不依赖 PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.multibook_consensus import (
    analyze_all, to_report, blend, LEISU_DB_PATH, DEFAULT_DIVERGE_PP,
)

# 方向建议最小置信(pp): retail 低估 sharp 共识超过此值才给方向建议
MIN_VALUE_PP = 1.5


def directional_call(m: dict) -> str:
    if not m["has_true_sharp"]:
        return "（无 true sharp 庄, 仅全庄代理共识, 不给出方向建议）"
    vs = m["value_side"]
    fs = m["fade_side"]
    if vs["pp"] >= MIN_VALUE_PP:
        return f"倾向价值侧 {vs['outcome']} (retail 低估 {vs['pp']}pp); 该 fade {fs['outcome']}"
    return f"离散不足/无明确方向 (价值侧 {vs['outcome']} 仅 +{vs['pp']}pp)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-mock", action="store_true")
    ap.add_argument("--pp", type=float, default=DEFAULT_DIVERGE_PP)
    args = ap.parse_args()

    res = analyze_all(diverge_pp=args.pp, include_mock=args.include_mock)
    report = to_report(res, args.pp)

    print(f"=== 多庄 sharp 共识验证 ({report['n_matches']} 场, sharp={report['sharp_books']}) ===\n")
    for m in report["matches"]:
        c = m["sharp_consensus"]
        print(f"• {m['home']} vs {m['away']}  [{m['n_books']}庄/{m['n_sharp']}sharp]")
        print(f"    sharp共识: H {c['h']*100:.1f}% / D {c['d']*100:.1f}% / A {c['a']*100:.1f}%  (离散 {m['max_spread_pp']}pp)")
        print(f"    方向建议: {directional_call(m)}")
        # 演示: sharp 共识作为 unified_predictor 的 market prior (w=0.7 市场锚定)
        p_market = [c["h"], c["d"], c["a"]]
        p_model = [1/3, 1/3, 1/3]  # 占位中性模型, 真实用 unified_predictor 输出
        final = blend(p_market, p_model, w=0.7)
        print(f"    市场锚定融合(w=0.7): H {final[0]*100:.1f}% / D {final[1]*100:.1f}% / A {final[2]*100:.1f}%")

    out = Path("data/multibook_consensus_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {out} | 含 true sharp 的场: {report['n_with_true_sharp']}/{report['n_matches']}")


if __name__ == "__main__":
    main()
