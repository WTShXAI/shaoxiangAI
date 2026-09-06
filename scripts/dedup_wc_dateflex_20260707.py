# -*- coding: utf-8 -*-
"""世界杯去重复 (2026-07-07, 用户批准):
问题: 上一轮 dedup 按 (home,away,match_date精确) 分组, 漏掉了 32 对"中文源早1天/英文源晚1天"的重复导入。
本次: 按 (canon_home, canon_away) 忽略日期差分组, keeper=有match_features的英文副本, 合并赛果, 清重复/孤儿特征, 重定向其他表引用, 删冗余。
开头: 用户纠正 USA/Belgium 真实比分 1-4 (537380 home_score=1, away_score=4)。
先整库备份, 事务执行, 强断言。
"""
import sqlite3, shutil, os, datetime, sys
from collections import defaultdict
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipeline"))
import wc_engine as W

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(PROJ, "data", "football_data.db")
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP = os.path.join(PROJ, "data", f"_backup_dedup_dateflex_{TS}.db")

assert os.path.exists(DB)
shutil.copy(DB, BACKUP)
print(f"[backup] {BACKUP}")

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; cur = con.cursor()

# 找所有含 match_id 列的表(用于引用重定向)
ref_tables = []
for t in cur.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    t = t["name"]
    if t in ("matches", "match_features", "sqlite_sequence"):
        continue
    cols = [c["name"] for c in cur.execute(f"PRAGMA table_info({t})")]
    if "match_id" in cols:
        ref_tables.append(t)
print(f"[ref_tables] {ref_tables}")

# 分组 (忽略日期差)
rows = cur.execute("SELECT * FROM matches WHERE league_name='世界杯'").fetchall()
mf = set(x[0] for x in cur.execute("SELECT DISTINCT match_id FROM match_features"))
groups = defaultdict(list)
for r in rows:
    ch, ca = W._canon_team(r["home_team_name"]), W._canon_team(r["away_team_name"])
    groups[(ch, ca)].append(r)

to_delete, feature_transfer, feature_delete, merges = [], [], [], []
for (ch, ca), lst in groups.items():
    if len(lst) <= 1:
        continue
    def sc(r):
        return (r["match_id"] in mf, str(r["match_id"]).startswith("537"), r["match_date"])
    lst_sorted = sorted(lst, key=sc, reverse=True)
    keeper = lst_sorted[0]
    for other in lst_sorted[1:]:
        # 合并赛果到 keeper (仅填空)
        for f in ("final_result", "home_score", "away_score", "status"):
            kv = keeper[f]; ov = other[f]
            if (kv is None or kv == "") and ov not in (None, ""):
                merges.append((keeper["match_id"], f, ov))
        # 特征处理
        if other["match_id"] in mf:
            if keeper["match_id"] in mf:
                feature_delete.append(other["match_id"])
            else:
                feature_transfer.append((other["match_id"], keeper["match_id"]))
        to_delete.append((other["match_id"], keeper["match_id"], f"{ch} vs {ca}"))

# 引用重定向检查
ref_issues = []
for mid, keeper, _ in to_delete:
    for t in ref_tables:
        cnt = cur.execute(f"SELECT COUNT(*) c FROM {t} WHERE match_id=?", (mid,)).fetchone()["c"]
        if cnt > 0:
            ref_issues.append((mid, keeper, t, cnt))

print(f"\n[plan] 删 {len(to_delete)} 行 | 特征转移 {len(feature_transfer)} | 特征删重 {len(feature_delete)} | 引用重定向 {len(ref_issues)}")
for mid, keeper, reason in to_delete:
    print(f"  删 {mid} -> 保留 {keeper}  ({reason})")

con.execute("BEGIN")
# 1. 引用重定向
for mid, keeper, t, cnt in ref_issues:
    cur.execute(f"UPDATE {t} SET match_id=? WHERE match_id=?", (keeper, mid))
    print(f"  [redirect] {t}: {mid}->{keeper} ({cnt}行)")
# 2. 特征转移 / 删重
for frm, to in feature_transfer:
    cur.execute("UPDATE match_features SET match_id=? WHERE match_id=?", (to, frm))
for frm in feature_delete:
    cur.execute("DELETE FROM match_features WHERE match_id=?", (frm,))
# 3. 合并赛果到 keeper
for kmid, f, val in merges:
    cur.execute(f"UPDATE matches SET {f}=? WHERE match_id=?", (val, kmid))
# 4. 用户纠正: 537380 真实比分 1-4
cur.execute("UPDATE matches SET home_score=1, away_score=4 WHERE match_id=537380")
# 5. 删除冗余 matches
for mid, keeper, _ in to_delete:
    cur.execute("DELETE FROM matches WHERE match_id=?", (mid,))
con.commit()
print("[committed]")

# ── 断言 ──
n_wc = cur.execute("SELECT COUNT(*) c FROM matches WHERE league_name='世界杯'").fetchone()["c"]
rows2 = cur.execute("SELECT match_id,home_team_name,away_team_name FROM matches WHERE league_name='世界杯'").fetchall()
g2 = defaultdict(int)
for r in rows2:
    g2[(W._canon_team(r["home_team_name"]), W._canon_team(r["away_team_name"]))] += 1
dups = [k for k, v in g2.items() if v > 1]
orphan = cur.execute("SELECT COUNT(*) c FROM match_features WHERE match_id NOT IN (SELECT match_id FROM matches)").fetchone()["c"]
b380 = cur.execute("SELECT home_score,away_score,final_result,status FROM matches WHERE match_id=537380").fetchone()
assert not dups, f"仍有重复组: {dups}"
assert orphan == 0, f"match_features 孤儿: {orphan}"
assert b380["home_score"] == 1 and b380["away_score"] == 4 and b380["final_result"] == "A", f"537380 比分应1-4/A: {b380}"
print(f"\n[OK] WC行数={n_wc} | 残留重复组=0 | 孤儿特征=0 | 537380={b380['home_score']}-{b380['away_score']} {b380['final_result']} {b380['status']}")
con.close()
print(f"[done] 备份: {BACKUP}")
