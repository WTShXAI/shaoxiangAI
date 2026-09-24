#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""KNN 赛前结论弹性写入器 (解耦于 ws_collector) — 2026-09-24 建立。

背景与根因:
  ws_collector 的 KNN 结论固化 (_capture_knn_conclusions_tick) 跑在采集器守护线程里。
  采集器在 2026-09 多次崩溃/换号死亡期间, 该线程随之停写 → scheduled 场在 "scheduled 阶段
  恰逢 tick 死" 的窗口完场后, 永远拿不到 KNN 结论 (query_match 对 finished 状态硬性 applicable=False,
  不可回填)。这就是 9 月 9444 场 finished+score 无结论缺口的主因 —— 诚实的永久样本损失。

职责 (单一、幂等、安全):
  扫描当前 status='scheduled' 且 prematch_conclusion 缺失 (且非虚拟联赛) 的场,
  调 query_match → store_prematch_conclusion 固化。与采集器 tick 完全同口径、同函数,
  故二者可并存 (upsert 幂等, 谁先写谁占, 后写不覆盖已冻结结论)。

为什么解耦而不是塞进 prod_guardian:
  prod_guardian 设计铁律 = 只读 events.db + 只拉起不杀/不写。本脚本是"业务写入",
  故作为独立 run-and-exit 进程由 prod_guardian 的 JOBS **代拉运行** (subprocess 委托),
  守护本身保持只读。

铁律遵守:
  · 不修改任何已训练模型
  · 不引入 IR-32 禁区字样 (cross_book / multibook / leyu_value_signal / bet_split_source /
    compute_value_layer / bet_core)
  · 只写 prematch_conclusion (采集器本来就会写的同一张表, 同口径) —— 不碰 matches / odds / 其他
  · 不回填 finished 场 (query_match 对 finished 硬性拒绝, 且回填=未来信息泄漏, 禁用)
  · 上限保护: 单轮最多处理 KNN_WRITE_LIMIT 场, 防单次长跑锁表

用法:
  python scripts/knn_conclusion_writer.py            # 跑一轮即退 (由 prod_guardian JOBS 代拉)
  python scripts/knn_conclusion_writer.py --limit 5  # 测试: 只处理前 5 场
"""
import os
import sys
import time

ROOT = r'D:\Architecture'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

KNN_WRITE_LIMIT = 200  # 单轮上限, 远超正常 scheduled 量 (<100), 仅防异常


def main():
    import sqlite3
    limit = KNN_WRITE_LIMIT
    if '--limit' in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index('--limit') + 1])
        except Exception:
            pass

    from pipeline.prematch_similarity import query_match, DEFAULT_K
    from gq.db import conn as _gqconn, store_prematch_conclusion, is_virtual_league

    now = time.time()
    with _gqconn(readonly=True) as c:
        c.row_factory = sqlite3.Row
        sched = c.execute(
            "SELECT match_key, league FROM matches WHERE status='scheduled'").fetchall()
        captured = {r["match_key"] for r in c.execute(
            "SELECT match_key FROM prematch_conclusion")}

    pending = [(mk, lg) for mk, lg in sched
               if mk not in captured and not is_virtual_league(lg or '')]
    if not pending:
        print(f"[KNN-WRITER] 无待固化 scheduled 场 (scheduled 共 {len(sched)} 场, 均已固化)")
        return

    n = 0
    skipped = 0
    for mk, lg in pending[:limit]:
        try:
            r = query_match(mk, k=DEFAULT_K, draw_upgrade=True)
        except Exception as e:
            print(f"[KNN-WRITER][WARN] {mk} query_match 异常: {e}")
            continue
        if not r.get('applicable'):
            skipped += 1
            continue
        try:
            store_prematch_conclusion(mk, r['verdict'], r['verdict_cn'],
                                      r.get('excess'), r.get('roi'),
                                      int(r.get('draw_alert') or 0))
            n += 1
        except Exception as e:
            print(f"[KNN-WRITER][WARN] {mk} 固化失败: {e}")
    print(f"[KNN-WRITER] 本轮固化 {n} 场 / 跳过不适用 {skipped} 场 "
          f"(待固化池 {len(pending)}, 耗时 {time.time()-now:.1f}s)")


if __name__ == '__main__':
    main()
