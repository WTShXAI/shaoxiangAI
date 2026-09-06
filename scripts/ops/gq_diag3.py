"""Read-only diagnostic of events.db + WAL. No writes. Establishes ground truth
for the WAL header repair (pagesize / salt / checksum offsets)."""
import sqlite3, os, time, struct

DB = "data/events.db"
WAL = DB + "-wal"

def hexdump(b, off=0):
    s = ""
    for i in range(0, len(b), 16):
        chunk = b[i:i+16]
        hexs = " ".join("%02x" % x for x in chunk)
        s += "  %04x: %-47s\n" % (off+i, hexs)
    return s

print("================ MAIN DB HEADER (first 100 bytes) ================")
with open(DB, "rb") as f:
    dbhdr = f.read(100)
print(hexdump(dbhdr, 0))
# SQLite main DB header: offset 16-17 = page size (big-endian 16-bit)
db_pagesize = int.from_bytes(dbhdr[16:18], "big")
print("  main DB page size (offset 16:18 big-endian) =", db_pagesize)
db_magic = dbhdr[0:16]
print("  main DB magic (should be 'SQLite format 3\\x00') =", db_magic)

print("\n================ WAL HEADER (first 56 bytes) ================")
with open(WAL, "rb") as f:
    wh = f.read(56)
print(hexdump(wh, 0))
magic = int.from_bytes(wh[0:4], "big")
psize = int.from_bytes(wh[4:8], "big")
seq   = int.from_bytes(wh[8:12], "big")
salt1 = int.from_bytes(wh[12:16], "big")
salt2 = int.from_bytes(wh[16:20], "big")
ck1   = int.from_bytes(wh[20:24], "big")
ck2   = int.from_bytes(wh[24:28], "big")
print("  magic       = %08x  (377f0682 = valid)" % magic)
print("  pagesize    = %d  (offset 4:8)" % psize)
print("  seq/rec     = %d  (offset 8:12)" % seq)
print("  salt1       = %08x  (offset 12:16)" % salt1)
print("  salt2       = %08x  (offset 16:20)" % salt2)
print("  cksum1      = %08x  (offset 20:24)" % ck1)
print("  cksum2      = %08x  (offset 24:28)" % ck2)

print("\n================ WAL FRAME 1 HEADER (offset 32..56) ================")
fh = wh[32:56]
pgno = int.from_bytes(fh[0:4], "big")
nsub = int.from_bytes(fh[4:8], "big")
fsalt1 = int.from_bytes(fh[8:12], "big")
fsalt2 = int.from_bytes(fh[12:16], "big")
fck1 = int.from_bytes(fh[16:20], "big")
fck2 = int.from_bytes(fh[20:24], "big")
print("  frame1 pgno    = %d  (offset 40:44)" % pgno)
print("  frame1 nsub    = %d  (offset 44:48)" % nsub)
print("  frame1 salt1   = %08x  (offset 48:52)" % fsalt1)
print("  frame1 salt2   = %08x  (offset 52:56)" % fsalt2)
print("  frame1 cksum1  = %08x  (offset 56:60)" % fck1)
print("  frame1 cksum2  = %08x  (offset 60:64)" % fck2)

print("\n================ WAL GROWTH TEST (sample twice, 3s apart) ================")
s1 = os.path.getsize(WAL)
t1 = time.time()
time.sleep(3)
s2 = os.path.getsize(WAL)
t2 = time.time()
print("  size t1=%d  t2=%d  delta=%d bytes in %.1fs" % (s1, s2, s2-s1, t2-t1))
print("  -> WAL %s" % ("IS GROWING (a writer is alive!)" if s2>s1 else "is static (no active writer)"))

print("\n================ INTEGRITY / CHECKPOINT via SQLite ================")
try:
    c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    try:
        r = c.execute("PRAGMA quick_check(20)").fetchall()
        print("  quick_check(20):", r)
    except Exception as e:
        print("  quick_check ERROR:", repr(e))
    c.close()
except Exception as e:
    print("  open(ro) ERROR:", repr(e))

try:
    c2 = sqlite3.connect(DB, timeout=60)
    c2.execute("PRAGMA busy_timeout=60000")
    r = c2.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print("  wal_checkpoint(TRUNCATE) ->", r)
    c2.close()
except Exception as e:
    print("  wal_checkpoint(TRUNCATE) ERROR:", repr(e))

print("\nDIAGNOSTIC DONE (read-only, no files modified)")
