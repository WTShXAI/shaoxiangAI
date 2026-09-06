"""诊断 events.db WAL: 对比主库/WAL 头部, 尝试 PASSIVE/FULL checkpoint 拿状态码。"""
import sqlite3, os, struct
DB = "data/events.db"; WAL = DB + "-wal"
def hx(b): return b.hex() if b else "NONE"
with open(DB, "rb") as f: dbh = f.read(100)
with open(WAL, "rb") as f: walh = f.read(32)
print("DB magic:", hx(dbh[:4]), "| DB pagesize field(16:18):", int.from_bytes(dbh[16:18], "big"))
print("WAL magic:", hx(walh[:4]), "| WAL pagesize(4:8):", int.from_bytes(walh[4:8], "big"))
print("WAL salt1:", hx(walh[12:16]), "salt2:", hx(walh[16:20]))
print("WAL checksum1:", hx(walh[20:24]), "checksum2:", hx(walh[24:28]))
print("WAL filesize:", os.path.getsize(WAL))
c = sqlite3.connect(DB, timeout=120)
c.execute("PRAGMA busy_timeout=120000")
for mode in ("PASSIVE", "FULL"):
    try:
        r = c.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        print(f"{mode} checkpoint ->", r, "(total,ckpted,status; 0=ok 1=busy 2=toofull)")
    except Exception as e:
        print(f"{mode} checkpoint ERROR:", repr(e))
try:
    rc = c.execute("PRAGMA wal_checkpoint").fetchone()
    print("requery status ->", rc)
except Exception as e:
    print("requery ERROR:", repr(e))
c.close()
