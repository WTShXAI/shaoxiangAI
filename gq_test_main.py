"""Test: open events.db with immutable=1 (IGNORES WAL/-shm entirely) to learn whether
the MAIN DB file is self-sufficient (integral + has the expected data) without the WAL.
Non-destructive: opens read-only, touches nothing on disk."""
import sqlite3, time

DB = "data/events.db"
print("Opening events.db with immutable=1 (WAL ignored) ...", flush=True)
t0 = time.time()
c = sqlite3.connect("file:%s?mode=ro&immutable=1" % DB, uri=True, timeout=30)
c.execute("PRAGMA busy_timeout=30000")
print("  opened in %.1fs" % (time.time()-t0), flush=True)

def q(sql):
    try:
        return c.execute(sql).fetchone()[0]
    except Exception as e:
        return "ERR:" + repr(e)

print("  integrity_check(20):", q("PRAGMA integrity_check(20)"), flush=True)
for tbl in ["cs_verification", "odds_snapshots", "matches", "pre_match_cs", "cs_calibration"]:
    n = q("SELECT COUNT(*) FROM %s" % tbl)
    print("  COUNT %-18s = %s" % (tbl, n), flush=True)

# sample a cs_verification row to confirm content present
try:
    r = c.execute("SELECT match_key, actual_score, actual_odds, hit, favorite_score FROM cs_verification LIMIT 3").fetchall()
    print("  sample cs_verification:", r, flush=True)
except Exception as e:
    print("  sample ERR:", repr(e), flush=True)

c.close()
print("MAIN-DB TEST DONE", flush=True)
