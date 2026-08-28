# -*- coding: utf-8 -*-
"""清理 events.db 隐患: 异常/重复快照 (2026-08-28 用户拍板后执行)。

清理范围:
  - 异常快照: odds<=1.01 或 odds>=1000 或 odds IS NULL (149,678 条, 0.45% of 33M)
  - 重复组: 同 (match_key, market, selection, captured_at) 留最早, 删后续 (100,320 组)
  - 不删 matches 表 (status 不一致 1470 条后续单独处理, 不丢数据)

影响模型:
  - fl_model (29 维 tick 特征, 训练用 odds_snapshots): 删的是异常/重复, 对它是噪音剔除
  - wi_1x2_model (598K football_data): 不依赖 events.db, 不受影响
  - live_1x2_model (live_rollball): 不依赖, 不受影响
  - cs_trust_model._empirical_scoreline_freq: 读 match_outcomes, 不受影响
  - ranked_predictor: 训练完成, 不重新训练

用法:
  python scripts/cleanup_events_db.py --dry-run   # 只统计, 不删
  python scripts/cleanup_events_db.py --execute  # 实际执行 (先备份)
"""
import sqlite3, os, sys, time, shutil

DB = r'D:\Architecture\data\events.db'
BAK = DB + '.bak_' + time.strftime('%Y%m%d_%H%M%S')


def stats(con):
    """统计待清理量."""
    cur = con.cursor()
    z1 = cur.execute("SELECT COUNT(*) FROM odds_snapshots WHERE odds<=1.01 OR odds>=1000 OR odds IS NULL").fetchone()[0]
    z2 = cur.execute("""SELECT IFNULL(SUM(c-1),0) FROM (
                          SELECT COUNT(*) c FROM odds_snapshots
                          GROUP BY match_key, market, selection, captured_at HAVING c>1)""").fetchone()[0]
    return z1, z2


def cleanup(con):
    """执行清理."""
    cur = con.cursor()
    # 1. 删异常快照
    n1 = cur.execute("DELETE FROM odds_snapshots WHERE odds<=1.01 OR odds>=1000 OR odds IS NULL").rowcount
    # 2. 删重复组 (保留 rowid 最小的)
    n2 = cur.execute("""DELETE FROM odds_snapshots WHERE rowid NOT IN (
                          SELECT MIN(rowid) FROM odds_snapshots
                          GROUP BY match_key, market, selection, captured_at)""").rowcount
    con.commit()          # 2026-08-28 修复: VACUUM 不能在事务内, 先提交删除
    cur.execute("VACUUM")
    return n1, n2


if __name__ == "__main__":
    mode = "--execute" if "--execute" in sys.argv else "--dry-run"
    if not os.path.exists(DB):
        print(f"DB 不存在: {DB}")
        sys.exit(1)
    con = sqlite3.connect(DB, timeout=60)
    z1, z2 = stats(con)
    total = z1 + z2
    print(f"待清理: 异常快照 {z1:,} + 重复 {z2:,} = {total:,}")
    if mode == "--dry-run":
        print("[dry-run] 不执行任何删除. 跑 `--execute` 实际执行(会先备份到 events.db.bak_*)")
        sys.exit(0)
    # 实际执行: 先备份
    print(f"备份 → {BAK}")
    shutil.copy2(DB, BAK)
    n1, n2 = cleanup(con)
    con.commit()
    con.close()
    print(f"完成: 删异常 {n1:,} + 删重复 {n2:,} = {n1+n2:,}")
    print(f"备份保留: {BAK}")