"""
scripts/backfill_prediction_ledger.py — 重建历史初盘预测并解析, 输出准确率 + 错误分布 + 优化触发状态。

用法:
  python scripts/backfill_prediction_ledger.py [--limit N] [--report]

默认 limit=200 (最近的已结束比赛)。仅回测用, 不修改任何初始分析/标签。
"""
from __future__ import annotations
import argparse
import sqlite3
import sys

sys.path.insert(0, "D:/Architecture")

from pipeline.prediction_ledger import (
    init_ledger,
    backfill,
    accuracy_report,
    optimization_status,
    OPTIMIZE_AT,
)

DB = "D:/Architecture/data/events.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--report-only", action="store_true",
                    help="只输出报告, 不回测 (用于查看已累积数据)")
    args = ap.parse_args()

    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    init_ledger(con)

    if not args.report_only:
        n_rec, n_res = backfill(con, limit=args.limit)
        print(f"[backfill] 新写入预测 {n_rec} 条, 解析 {n_res} 条 (limit={args.limit})")

    print("\n=== 准确率 (correct=1 对 / 0 错 / 2 走盘) ===")
    for market, right, wrong, push, total in accuracy_report(con):
        if not total:
            continue
        acc = right / total * 100
        print(f"  {market:7} 对{right:4} 错{wrong:4} 走{push:3} / 共{total:4}  准确率 {acc:5.1f}%")

    print(f"\n=== 错误分布 + 优化触发 (阈值 OPTIMIZE_AT={OPTIMIZE_AT}) ===")
    status = optimization_status(con)
    if not status:
        print("  暂无已解析的错误记录")
    else:
        for s in status:
            flag = "⚠ 可优化" if s["ready"] else "  累积中"
            print(f"  {flag} {s['market']:7} {s['reason_category']:22} 错{s['wrong_count']:4}/{s['threshold']}")

    con.close()


if __name__ == "__main__":
    main()
