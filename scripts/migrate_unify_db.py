#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B2 统一数据库迁移 (2026-09-19 架构升级): 把训练关键表蒸馏迁入 events.db 统一库。
================================================================================
迁移表:
  football_data.db::historical_matches (+4索引) — KNN 生产库
  football_data.db::odds_features    (+3索引) — 历史特征
  rollball_training.db::rb_matches            — 滚球训练集 (31.9万)

幂等: 目标表已存在且行数一致则跳过。迁移后:
  - prematch_similarity.FOOTBALL_DB 默认指 events.db
  - rollball_training.db 归档 archive/db_migrated_20260919/
  - football_data.db 留作研究兼容 (其余表未迁移, 退役列入 backlog)

用法: python scripts/migrate_unify_db.py [--verify-only]
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, r'D:\Architecture')
EVENTS = r'D:\Architecture\data\events.db'
FOOT = r'D:\Architecture\data\football_data.db'
ROLL = r'D:\Architecture\data\rollball_training.db'

JOBS = [
    (FOOT, 'historical_matches'),
    (FOOT, 'odds_features'),
    (ROLL, 'rb_matches'),
]


def copy_table(dst: sqlite3.Connection, src_path: str, table: str, verify_only: bool):
    src = sqlite3.connect(f'file:{src_path}?mode=ro', uri=True)
    n_src = src.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone()
    idxs = [r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,))]
    src.close()
    have = dst.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if have:
        n_dst = dst.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        if n_dst == n_src:
            print(f'  {table}: 已存在且行数一致 ({n_src}), 跳过')
            return
        print(f'  {table}: 目标 {n_dst} != 源 {n_src} → 重建')
        dst.execute(f'DROP TABLE {table}')
    if verify_only:
        print(f'  {table}: 源 {n_src} 行, 目标缺失')
        return
    dst.execute(ddl[0])
    src_ro = sqlite3.connect(f'file:{src_path}?mode=ro', uri=True)
    src_ro.attach('file:' + EVENTS.replace('\\', '/') + '?mode=ro', 'dst_db') if False else None
    src_ro.close()
    # 跨库批量: 用 ATTACH 到目标连接读源
    dst.execute("ATTACH DATABASE ? AS src_db", (src_path,))
    cols = [r[1] for r in dst.execute(f'PRAGMA table_info({table})')]
    col_list = ','.join(cols)
    dst.execute(f'INSERT INTO main.{table} ({col_list}) SELECT {col_list} FROM src_db.{table}')
    dst.commit()  # 先提交再 DETACH (事务横跨两库, 不提交 DETACH 会 locked)
    dst.execute('DETACH DATABASE src_db')
    for idx_sql in idxs:
        try:
            dst.execute(idx_sql)
        except Exception as e:
            print(f'  索引跳过: {e}')
    n_new = dst.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    print(f'  {table}: {n_src} → {n_new} 行 {"OK" if n_new == n_src else "!!行数不一致!!"}')


def main():
    verify_only = '--verify-only' in sys.argv
    t0 = time.time()
    # 走 gq 单写者管理器 (避免与采集器/回填抢锁, 2026-09-19)
    from gq.db import conn as gq_conn_cm
    dst_cm = gq_conn_cm()
    dst = dst_cm.__enter__()
    for src_path, table in JOBS:
        print(f'== {os.path.basename(src_path)}::{table}')
        copy_table(dst, src_path, table, verify_only)
    if not verify_only:
        dst.execute("""CREATE TABLE IF NOT EXISTS db_unification_meta (
            key TEXT PRIMARY KEY, value TEXT)""")
        dst.execute("INSERT OR REPLACE INTO db_unification_meta VALUES ('unified_at', datetime('now'))")
        dst.execute("""INSERT OR REPLACE INTO db_unification_meta VALUES ('sources',
            'football_data.db{historical_matches,odds_features} rollball_training.db{rb_matches}')""")
    dst_cm.__exit__(None, None, None)
    print(f'完成 [{time.time()-t0:.0f}s]')


if __name__ == '__main__':
    main()
