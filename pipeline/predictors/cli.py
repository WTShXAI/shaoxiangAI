"""Full Linkage Predictor — 拆分子模块"""
import os, sys, json, math
import logging
from typing import Dict, List, Tuple, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

from ._compat import np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from pipeline.predictors.pipeline import *  # noqa: F401, F403

def run_6_27_full_linkage():
    """批量运行6/27全链路联动分析"""
    pipeline = FullLinkagePipeline()
    results = []

    for m in MATCHES_6_27:
        result = pipeline.predict(m)
        results.append(result)
        print()

    # ── 汇总: 5串1方案 ──
    print(f"\n{'='*60}")
    print(f"  🎫 6/27 全链路联动 • 5串1推荐方案")
    print(f"{'='*60}")

    picks = []
    for r in results:
        primary = r['final_verdict']['primary']
        secondary = r['final_verdict']['secondary']
        conf = r['final_verdict']['confidence']

        # 寻找对应赔率
        m = r['match']
        print(f"  {m:30s} → {primary}+{secondary:4s} "
              f"| 比分: {r['final_verdict']['best_score']:<5s} "
              f"| conf={conf:.2f}")

    return results

# ════════════════════════════════════════════════════
# CLI入口
# ════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='FootballAI 全链路联动预测管道')
    parser.add_argument('--batch', action='store_true', help='批量分析6/27全部比赛')
    parser.add_argument('--match', type=str, help='单场分析 (格式: 主队vs客队)')
    parser.add_argument('--hcp', type=float, default=0, help='让球')
    parser.add_argument('--ou', type=float, default=2.5, help='大小球盘口')
    parser.add_argument('--odds', type=str, default='2.0,3.5,3.5', help='1X2赔率 h,d,a')
    parser.add_argument('--r3', action='store_true', help='R3轮换标记')

    args = parser.parse_args()

    if args.batch:
        run_6_27_full_linkage()
    elif args.match:
        teams = args.match.split('vs')
        oh, od, oa = map(float, args.odds.split(','))
        match = MatchInput(
            home=teams[0], away=teams[1],
            odds_h=oh, odds_d=od, odds_a=oa,
            hcp=args.hcp, ou_line=args.ou,
            r3_rotation=args.r3,
        )
        pipeline = FullLinkagePipeline()
        result = pipeline.predict(match)
        print(f"\n{'='*60}")
        print(json.dumps(result['final_verdict'], ensure_ascii=False, indent=2))
    else:
        print("用法: python full_linkage_predictor.py --batch  或  --match 挪威vs法国 --odds 4.05,3.55,1.80 --hcp 0.5 --ou 2.25")
