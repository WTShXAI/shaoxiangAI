#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性迁移: 把 events.db 从上一轮 event_db 旧结构(odds/results/content)
转换为 GQ.db 同名表结构, 并合并 GQ.db 全部历史数据。
跑完即弃。events.db 最终 = GQ.db 完整表结构 + interface_doc + h2h。"""
import sqlite3, os, sys, time
sys.path.insert(0, '.')
import gq.event_db as ed

t0 = time.time()
print("=== [1/3] DROP 上一轮 event_db 旧表 (matches/odds/results/content) ===", flush=True)
c = ed.conn()
c.executescript("""
DROP TABLE IF EXISTS matches;
DROP TABLE IF EXISTS odds;
DROP TABLE IF EXISTS results;
DROP TABLE IF EXISTS content;
""")
c.commit(); c.close()

print("=== [2/3] init_event_db (建 GQ 同名表 + interface_doc + h2h) ===", flush=True)
ed.init_event_db()
print("after init:", ed.stats(), flush=True)

print("=== [3/3] backfill_all_from_gq(recreate=True) (合并 GQ.db 全部表, 按 GQ 实际 DDL 重建漂移表) ===", flush=True)
bf = ed.backfill_all_from_gq(recreate=True)
for k, v in sorted(bf.items()):
    print(f"  {k}: {v}", flush=True)

print("=== FINAL stats ===", flush=True)
print(ed.stats(), flush=True)
print("took %.1fs" % (time.time() - t0), flush=True)
