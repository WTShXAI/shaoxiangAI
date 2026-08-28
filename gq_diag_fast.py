"""FAST read-only diagnostic: only byte layout + WAL growth. No slow SQLite ops.
Flushes every print so we see progress even if a later step hangs."""
import os, time, sys

def log(*a):
    print(*a, flush=True)

DB = "data/events.db"
WAL = DB + "-wal"

def hexdump(b, off=0):
    s = ""
    for i in range(0, len(b), 16):
        chunk = b[i:i+16]
        hexs = " ".join("%02x" % x for x in chunk)
        s += "  %04x: %-47s\n" % (off+i, hexs)
    return s

log("================ MAIN DB HEADER (first 100 bytes) ================")
with open(DB, "rb") as f:
    dbhdr = f.read(100)
log(hexdump(dbhdr, 0))
db_pagesize = int.from_bytes(dbhdr[16:18], "big")
log("  main DB page size (offset 16:18 BE) =", db_pagesize)

log("\n================ WAL HEADER (first 64 bytes) ================")
with open(WAL, "rb") as f:
    wh = f.read(64)
log(hexdump(wh, 0))
magic = int.from_bytes(wh[0:4], "big")
psize = int.from_bytes(wh[4:8], "big")
seq   = int.from_bytes(wh[8:12], "big")
salt1 = int.from_bytes(wh[12:16], "big")
salt2 = int.from_bytes(wh[16:20], "big")
ck1   = int.from_bytes(wh[20:24], "big")
ck2   = int.from_bytes(wh[24:28], "big")
log("  magic    = %08x (377f0682 valid)" % magic)
log("  pagesize = %d  (offset 4:8)" % psize)
log("  seq/rec  = %d  (offset 8:12)" % seq)
log("  salt1    = %08x (offset 12:16)" % salt1)
log("  salt2    = %08x (offset 16:20)" % salt2)
log("  cksum1   = %08x (offset 20:24)" % ck1)
log("  cksum2   = %08x (offset 24:28)" % ck2)

log("\n================ WAL FRAME 1 HEADER (offset 32..64) ================")
fh = wh[32:64]
pgno  = int.from_bytes(fh[0:4], "big")
nsub  = int.from_bytes(fh[4:8], "big")
fsalt1= int.from_bytes(fh[8:12], "big")
fsalt2= int.from_bytes(fh[12:16], "big")
fck1  = int.from_bytes(fh[16:20], "big")
fck2  = int.from_bytes(fh[20:24], "big")
log("  frame1 pgno   = %d  (offset 40:44)" % pgno)
log("  frame1 nsub   = %d  (offset 44:48)" % nsub)
log("  frame1 salt1  = %08x (offset 48:52)" % fsalt1)
log("  frame1 salt2  = %08x (offset 52:56)" % fsalt2)
log("  frame1 cksum1 = %08x (offset 56:60)" % fck1)
log("  frame1 cksum2 = %08x (offset 60:64)" % fck2)

log("\n================ WAL GROWTH (2 quick samples) ================")
s1 = os.path.getsize(WAL); t1 = time.time()
time.sleep(3)
s2 = os.path.getsize(WAL); t2 = time.time()
log("  size t1=%d t2=%d delta=%d in %.1fs" % (s1, s2, s2-s1, t2-t1))
log("  -> WAL %s" % ("IS GROWING (writer alive!)" if s2>s1 else "static (no active writer)"))

log("\nFAST DIAGNOSTIC DONE")
