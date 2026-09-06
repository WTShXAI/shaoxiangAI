"""Read events.db-shm (WAL-index) header to extract the AUTHORITATIVE salt+checksum
that the WAL frames were written against. This is the source of truth for repairing
the corrupt events.db-wal header (reads work via -shm, but checkpoint re-reads -wal header).

WalIndexHdr layout (sqlite wal.c):
  [0:4]   iPageSize
  [4:8]   iPageFormat
  [8:12]  nPage
  [12:16] nSize
  [16:20] iRoot
  [20:24] nMaxFrame
  [24:28] nMinFrame
  [28:32] aFrame[0]
  [32:36] aFrame[1]
  [36:40] szPage
  [40:44] aSalt[0]
  [44:48] aSalt[1]
  [48:52] aCksum[0]
  [52:56] aCksum[1]
"""
import os

SHM = "data/events.db-shm"
WAL = "data/events.db-wal"

def g(b, a, b_):
    return int.from_bytes(b[a:b_], "big")

print("===== SHM WalIndexHdr (first 64 bytes) =====")
with open(SHM, "rb") as f:
    sh = f.read(64)
for i in range(0, 64, 4):
    print("  [%2d:%2d] = %08x" % (i, i+4, g(sh, i, i+4)))

iPageSize = g(sh, 0, 4)
iPageFormat = g(sh, 4, 8)
aSalt0 = g(sh, 40, 44)
aSalt1 = g(sh, 44, 48)
aCksum0 = g(sh, 48, 52)
aCksum1 = g(sh, 52, 56)
nMaxFrame = g(sh, 20, 24)
nMinFrame = g(sh, 24, 28)
print("\n  -> iPageSize =", iPageSize)
print("  -> iPageFormat =", iPageFormat, "(should be like 3007000 / version)")
print("  -> aSalt    = (%08x, %08x)" % (aSalt0, aSalt1))
print("  -> aCksum   = (%08x, %08x)" % (aCksum0, aCksum1))
print("  -> nMaxFrame =", nMaxFrame, " nMinFrame =", nMinFrame)

print("\n===== -wal header current (first 28 bytes) =====")
with open(WAL, "rb") as f:
    wh = f.read(28)
w_magic = g(wh, 0, 4)
w_psize = g(wh, 4, 8)
w_seq   = g(wh, 8, 12)
w_salt0 = g(wh, 12, 16)
w_salt1 = g(wh, 16, 20)
w_ck0   = g(wh, 20, 24)
w_ck1   = g(wh, 24, 28)
print("  magic  = %08x" % w_magic)
print("  psize  = %d" % w_psize)
print("  seq    = %d" % w_seq)
print("  salt   = (%08x, %08x)" % (w_salt0, w_salt1))
print("  cksum  = (%08x, %08x)" % (w_ck0, w_ck1))

print("\n===== SANITY: does shm salt match frame1 salt? =====")
with open(WAL, "rb") as f:
    fh = f.read(56)
f_salt0 = g(fh, 48, 52)
f_salt1 = g(fh, 52, 56)
print("  frame1 salt = (%08x, %08x)" % (f_salt0, f_salt1))
print("  shm   salt = (%08x, %08x)" % (aSalt0, aSalt1))
print("  MATCH" if (f_salt0, f_salt1) == (aSalt0, aSalt1) else "  MISMATCH (shm not authoritative for salt!)")

print("\nSHM READ DONE (no writes)")
