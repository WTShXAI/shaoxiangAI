"""一次性迁移: 存量 CS 比分标签 hyphen/dot → 英文冒号 '0:0' 格式。

仅处理小表, 不涉及 7GB odds_snapshots 全表扫描:
  - cs_verification.actual_score / favorite_score: REPLACE('-',':')
  - cs_verification.pre_odds_json: JSON 键归一
  - pre_match_cs.odds_json: JSON 键归一
读取端(_pre_market_batch / _get_cs_grid)已做读时归一, 此处为存储一致性。
"""
import sqlite3, json, time
from gq.db import normalize_cs_score, DB_PATH

con = sqlite3.connect(DB_PATH, timeout=30)
con.execute('PRAGMA busy_timeout=30000')
cur = con.cursor()

# 1) cs_verification 标量列
for col in ('actual_score', 'favorite_score'):
    before = cur.execute(
        f"SELECT COUNT(*) FROM cs_verification WHERE {col} LIKE '%-%'").fetchone()[0]
    cur.execute(
        f"UPDATE cs_verification SET {col}=REPLACE({col},'-',':') WHERE {col} LIKE '%-%'")
    after = cur.execute(
        f"SELECT COUNT(*) FROM cs_verification WHERE {col} LIKE '%-%'").fetchone()[0]
    print(f"cs_verification.{col}: hyphen行 {before} -> {after} (应归零)")
con.commit()

# 2) JSON 键归一 (cs_verification.pre_odds_json + pre_match_cs.odds_json)
def remap_json(obj):
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        nk = normalize_cs_score(k)
        out[nk if nk is not None else k] = v
    return out

total = 0
for tbl, col in (('cs_verification', 'pre_odds_json'), ('pre_match_cs', 'odds_json')):
    rows = cur.execute(f"SELECT rowid, {col} FROM {tbl} WHERE {col} IS NOT NULL").fetchall()
    fixed = 0
    for rid, oj in rows:
        try:
            d = json.loads(oj)
        except Exception:
            continue
        nd = remap_json(d)
        if nd != d:
            cur.execute(f"UPDATE {tbl} SET {col}=? WHERE rowid=?",
                        (json.dumps(nd, ensure_ascii=False), rid))
            fixed += 1
    con.commit()
    total += fixed
    print(f"{tbl}.{col}: {len(rows)} 行, 归一 {fixed} 行")
print(f"\nJSON 键归一完成, 共 {total} 行")
