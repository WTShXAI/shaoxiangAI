"""决定性测试: odds_snapshots(在WAL中) 是否可读/可写。 不 checkpoint, 仅测试读写。"""
import sqlite3, time, os
DB = "data/events.db"
c = sqlite3.connect(DB, timeout=120)
c.execute("PRAGMA busy_timeout=120000")
c.row_factory = sqlite3.Row

# 1) 读小表(主库) 确认可用
t=time.time(); n=c.execute("SELECT COUNT(*) FROM cs_verification").fetchone()[0]
print(f"[read small] cs_verification={n}  ({time.time()-t:.1f}s)")

# 2) 读 odds_snapshots 样本(可能走 WAL)
t=time.time()
rows=c.execute("SELECT id, match_key, market, selection, odds FROM odds_snapshots WHERE market='CS' LIMIT 3").fetchall()
print(f"[read CS sample] {len(rows)} rows ({time.time()-t:.1f}s)")
for r in rows: print("   ", dict(r))

# 3) 测试写: 对 matches 第一行做 no-op UPDATE, 提交, 看是否报 I/O error
mk = c.execute("SELECT match_key FROM matches LIMIT 1").fetchone()[0]
t=time.time()
try:
    c.execute("UPDATE matches SET first_seen=first_seen WHERE match_key=?", (mk,))
    c.commit()
    print(f"[write no-op UPDATE] OK committed ({time.time()-t:.1f}s)")
except Exception as e:
    print(f"[write] ERROR: {repr(e)}")

# 4) 测试写 odds_snapshots: 插一条测试行再删(用独立事务, 最后清理)
c.execute("BEGIN")
try:
    c.execute("INSERT INTO odds_snapshots(match_key, captured_at, market, selection, odds, line, score_at, minute_at) VALUES(?,?,?,?,?,?,?,?)",
              ("__rwtest__", 1.0, "CS", "0:0", 99.0, 0, "", 0))
    c.commit()
    print("[write insert CS] OK")
    c.execute("DELETE FROM odds_snapshots WHERE match_key='__rwtest__'")
    c.commit()
    print("[write delete test row] OK cleaned")
except Exception as e:
    print(f"[write odds_snapshots] ERROR: {repr(e)}")
    try: c.rollback()
    except: pass

c.close()
print("TEST DONE")
