"""Repair events.db-wal header using the AUTHORITATIVE salt+cksum from events.db-shm,
then checkpoint to fold the 35GB of good frames into the (currently malformed)
main DB. Fully reversible: backs up events.db + events.db-wal + events.db-shm first.

Approach:
- The -wal header is corrupt: pagesize=3007000 (illegal), salt/cksum wrong.
- Reads work only because -shm holds the correct salt+cksum the frames use.
- The shm layout empirically: salt at shm[32:40], cksum at shm[40:48].
- We copy those 16 bytes verbatim into -wal header[12:28] (salt+cksum),
  set pagesize=4096 (matches main DB), keep magic + seq.
- Then wal_checkpoint(TRUNCATE). SQLite is the authority: if it succeeds,
  integrity_check passes and WAL shrinks. On any failure we restore.
"""
import sqlite3, os, shutil, time

DB = "data/events.db"
WAL = DB + "-wal"
SHM = DB + "-shm"
BAK_DB  = DB + ".repair_bak"
BAK_WAL = WAL + ".repair_bak"
BAK_SHM = SHM + ".repair_bak"

def log(*a):
    print(*a, flush=True)

# ---------- 1. BACKUP ----------
log("[1] Backing up DB + WAL + SHM (reversible)...")
for src, dst in [(DB, BAK_DB), (WAL, BAK_WAL), (SHM, BAK_SHM)]:
    if not os.path.exists(dst):
        t0 = time.time()
        shutil.copyfile(src, dst)
        log("    backed up %s -> %s  (%d bytes, %.1fs)" % (src, dst, os.path.getsize(dst), time.time()-t0))
    else:
        log("    skip (already exists):", dst)

# ---------- 2. EXTRACT AUTHORITATIVE salt+cksum FROM SHM ----------
log("[2] Reading authoritative salt+cksum from -shm ...")
with open(SHM, "rb") as f:
    shm = f.read(64)
salt_bytes  = shm[32:40]   # 8 bytes: salt0, salt1
cksum_bytes = shm[40:48]   # 8 bytes: cksum0, cksum1
salt0 = int.from_bytes(salt_bytes[0:4], "big")
salt1 = int.from_bytes(salt_bytes[4:8], "big")
ck0   = int.from_bytes(cksum_bytes[0:4], "big")
ck1   = int.from_bytes(cksum_bytes[4:8], "big")
log("    shm salt  = (%08x, %08x)" % (salt0, salt1))
log("    shm cksum = (%08x, %08x)" % (ck0, ck1))

# ---------- 3. PATCH -wal HEADER ----------
log("[3] Patching -wal header ...")
with open(WAL, "r+b") as f:
    hdr = bytearray(f.read(32))
log("    original header[0:32] = %s" % hdr.hex())
# magic [0:4] keep; pagesize [4:8]=4096; seq [8:12] keep
hdr[4:8]   = (4096).to_bytes(4, "big")
hdr[12:20] = salt_bytes    # salt
hdr[20:28] = cksum_bytes  # cksum
log("    patched  header[0:32] = %s" % hdr.hex())
f.seek(0)
f.write(hdr)
f.flush()
os.fsync(f.fileno())
log("    wrote patched header (32 bytes)")

# ---------- 4. ATTEMPT CHECKPOINT ----------
log("[4] Attempting wal_checkpoint(TRUNCATE) ...")
ok = False
try:
    c = sqlite3.connect(DB, timeout=600)
    c.execute("PRAGMA busy_timeout=600000")
    r = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    log("    checkpoint result:", r)
    c.close()
    ok = True
except Exception as e:
    log("    checkpoint ERROR:", repr(e))
    ok = False

# ---------- 5. VERIFY ----------
if ok:
    log("[5] Verifying post-checkpoint DB (WAL now folded) ...")
    try:
        c2 = sqlite3.connect(DB, timeout=120)
        c2.execute("PRAGMA busy_timeout=120000")
        ic = c2.execute("PRAGMA integrity_check(20)").fetchall()
        log("    integrity_check(20):", ic)
        ncsv = c2.execute("SELECT COUNT(*) FROM cs_verification").fetchone()[0]
        noss = c2.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        log("    cs_verification=%d  odds_snapshots=%d" % (ncsv, noss))
        wal_size = os.path.getsize(WAL)
        log("    WAL size now = %d bytes" % wal_size)
        c2.close()
        integral = (ic == [("ok",)] or (len(ic)==1 and ic[0][0]=="ok"))
        if integral and wal_size < 10_000_000:
            log("REPAIR_OK: DB integral, WAL truncated (%d bytes), data present." % wal_size)
        else:
            log("REPAIR_INCOMPLETE: integrity=%s wal=%d -> restoring" % (ic, wal_size))
            ok = False
    except Exception as e:
        log("    verify ERROR:", repr(e))
        ok = False

# ---------- 6. RESTORE ON FAILURE ----------
if not ok:
    log("[6] Restoring from backup ...")
    for dst, src in [(DB, BAK_DB), (WAL, BAK_WAL), (SHM, BAK_SHM)]:
        if os.path.exists(src):
            shutil.copyfile(src, dst)
            log("    restored", dst)
    # also re-remove any -wal/-shm leftover? keep backups
    log("RESTORED (no changes applied). You may inspect and retry.")
else:
    log("[done] Repair succeeded. Backups kept at:")
    log("   ", BAK_DB)
    log("   ", BAK_WAL)
    log("   ", BAK_SHM)
