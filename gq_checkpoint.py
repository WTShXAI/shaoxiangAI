"""维护窗口: 对 events.db 做 WAL checkpoint(TRUNCATE), 折叠 WAL 回主库并清空。
需要无其它连接持有 DB 快照; 若 bridge 持连接导致无法截断, 返回状态会指示。
"""
import sqlite3, os, time
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "events.db")
def wal():
    p = DB + "-wal"
    return os.path.getsize(p) if os.path.exists(p) else 0
before = wal()
t0 = time.time()
c = sqlite3.connect(DB, timeout=120)
c.execute("PRAGMA busy_timeout=120000")
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
row = c.execute("PRAGMA wal_checkpoint").fetchone()
c.commit(); c.close()
after = wal()
print("WAL before bytes :", before, "(%.2f GB)" % (before/1e9))
print("WAL after  bytes :", after,  "(%.2f GB)" % (after/1e9))
print("checkpoint status :", row, "(busy,log,ckpt; 0=ok truncated, 1=busy, 2=too_full)")
print("elapsed sec      :", round(time.time()-t0, 1))
