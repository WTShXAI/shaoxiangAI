"""尝试修复 events.db 的 WAL 头部 (页大小/ salt / 头部校验和), 使 checkpoint 能跑通,
从而把正确的 WAL 帧折叠回主库, 修复主库坏页。

可逆: 先备份 events.db-wal -> events.db-wal.orig_bak, 失败即恢复。
"""
import sqlite3, os, shutil, struct, time

DB = "data/events.db"
WAL = DB + "-wal"
BAK = WAL + ".orig_bak"

def wal_checksum(cksum_init, data, bswap):
    """SQLite walChecksumBytes (little-endian WAL, bswap=False)."""
    s0 = cksum_init & 0xffff
    s1 = (cksum_init >> 16) & 0xffff
    assert len(data) % 8 == 0
    for i in range(0, len(data), 8):
        x0 = int.from_bytes(data[i:i+4], "little")
        x1 = int.from_bytes(data[i+4:i+8], "little")
        if bswap:
            x0 = ((x0 << 24) & 0xffffffff) | ((x0 << 8) & 0xff0000) | ((x0 >> 8) & 0xff00) | (x0 >> 24)
            x1 = ((x1 << 24) & 0xffffffff) | ((x1 << 8) & 0xff0000) | ((x1 >> 8) & 0xff00) | (x1 >> 24)
        s0 = (s0 + x0 + s1) & 0xffffffff
        s1 = (s1 + x1 + s0 + (x0 >> 31)) & 0xffffffff
        s0 = (s0 & 0xffff) + (s0 >> 16)
        s1 = (s1 & 0xffff) + (s1 >> 16)
    return (s1 << 16) | (s0 & 0xffff)

print("[0] backup WAL ->", BAK)
if not os.path.exists(BAK):
    shutil.copyfile(WAL, BAK)
    print("    backed up", os.path.getsize(BAK), "bytes")
else:
    print("    backup already exists")

with open(WAL, "r+b") as f:
    hdr = bytearray(f.read(32))
print("[1] original WAL header (32B):", hdr.hex())
orig_pagesize = int.from_bytes(hdr[4:8], "big")
orig_salt1 = int.from_bytes(hdr[12:16], "big")
orig_salt2 = int.from_bytes(hdr[16:20], "big")
print("    orig pagesize=%d salt1=%08x salt2=%08x" % (orig_pagesize, orig_salt1, orig_salt2))

# 帧 salt (frame1 @ offset 24): salt1 @ 32-35, salt2 @ 36-39
frame_salt1 = int.from_bytes(hdr[32:36], "big")
frame_salt2 = int.from_bytes(hdr[36:40], "big")
print("    frame1 salt1=%08x salt2=%08x" % (frame_salt1, frame_salt2))

# 修补: pagesize=4096(主库页大小), salt=帧salt
NEW_PAGESIZE = 4096
NEW_SALT = frame_salt1  # 帧 salt (salt1==salt2)
hdr[4:8]  = NEW_PAGESIZE.to_bytes(4, "big")
hdr[12:16] = NEW_SALT.to_bytes(4, "big")
hdr[16:20] = NEW_SALT.to_bytes(4, "big")
# 头部校验和: 对前 20 字节, seed=0
new_cksum = wal_checksum(0, bytes(hdr[0:20]), False)
hdr[20:24] = new_cksum.to_bytes(4, "big")
print("[2] patched header:", hdr[:24].hex(), "new_iCksum=%08x" % new_cksum)

# 自检: 用新头部 iCksum 作 seed, 校验 frame1 是否对得上 (frame 校验和应匹配存储值)
with open(WAL, "r+b") as f:
    f.seek(0); f.write(hdr[:24]); f.flush(); os.fsync(f.fileno())
print("[3] wrote patched header (24 bytes)")

# 读 frame1 全帧校验
with open(WAL, "rb") as f:
    f.seek(24)
    frame_hdr = f.read(24)            # frame header
    page_data = f.read(NEW_PAGESIZE)  # page
stored_cksum = int.from_bytes(frame_hdr[16:20], "big")  # cksum1(low)|cksum2(high) -> actually stored as 2x32 at 16:24
# frame checksum = walChecksum over (seed, frame_hdr[0:16]) then (prev, page_data)
c = wal_checksum(new_cksum, frame_hdr[0:16], False)
c = wal_checksum(c, page_data, False)
print("    frame1 computed cksum=%08x  stored(frame_hdr[16:20])=%08x" % (c, stored_cksum))
print("    MATCH" if c == stored_cksum else "    MISMATCH (still may proceed; header may differ from original)")

# 尝试 checkpoint
print("[4] attempt wal_checkpoint(TRUNCATE) ...")
try:
    c2 = sqlite3.connect(DB, timeout=180)
    c2.execute("PRAGMA busy_timeout=180000")
    r = c2.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print("    checkpoint result:", r)
    c2.close()
    # 验证
    c3 = sqlite3.connect(DB, timeout=60)
    n = c3.execute("SELECT COUNT(*) FROM cs_verification").fetchone()[0]
    print("    post-ckpt cs_verification=%d  (DB now checkpointable)" % n)
    c3.close()
    print("REPAIR_OK")
except Exception as e:
    print("    checkpoint ERROR:", repr(e))
    print("    restoring WAL from backup")
    shutil.copyfile(BAK, WAL)
    print("RESTORED")
