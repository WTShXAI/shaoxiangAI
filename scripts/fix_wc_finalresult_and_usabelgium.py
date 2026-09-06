# -*- coding: utf-8 -*-
"""修复 WC 数据质量 (2026-07-07):
1. 3场 finished 但 final_result=None(仅比分) 补 H/D/A 编码
   - 537418 Netherlands 3-4 Morocco -> A (客胜)
   - 537425 Mexico 2-0 Ecuador       -> H (主胜)
   - 537421 USA 2-0 Bosnia-H.        -> H (主胜)
2. USA vs Belgium 校正:
   - 537380 (英文, 有特征, scheduled) = 真实赛程 -> 填真实赛果 美国1-3比利时 (finished, A, 1-3)
   - 2130601 (中文源, 无特征, finished D0-0 占位) = 幻影 -> 删除
   (注: 2130601/537380 是'中文源07-06 / 英文源07-07'差1天的同一场重复导入, 与项目其他4对重复模式一致)
先整库备份, 事务写入, 强制断言。
"""
import sqlite3, shutil, os, datetime, sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(PROJ, "data", "football_data.db")
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP = os.path.join(PROJ, "data", f"_backup_fix_fr_usabelgium_{TS}.db")

assert os.path.exists(DB), f"DB 不存在: {DB}"
shutil.copy(DB, BACKUP)
print(f"[backup] {BACKUP}")

con = sqlite3.connect(DB); con.row_factory = sqlite3.Row; cur = con.cursor()

# ── 前置断言: 修复前状态符合预期 ──
pre = cur.execute("SELECT match_id,final_result,home_score,away_score,status FROM matches WHERE match_id IN (537418,537425,537421,537380,2130601)").fetchall()
pre_map = {r['match_id']: r for r in pre}
assert pre_map[537418]['final_result'] is None, "537418 前置应为 None"
assert pre_map[537425]['final_result'] is None, "537425 前置应为 None"
assert pre_map[537421]['final_result'] is None, "537421 前置应为 None"
assert pre_map[537380]['status'] == 'scheduled', "537380 前置应为 scheduled"
assert pre_map[2130601]['match_id'] == 2130601, "2130601 应存在"
print("[pre-check] OK")

con.execute("BEGIN")
# 1. 补 final_result 编码
cur.execute("UPDATE matches SET final_result='A' WHERE match_id=537418")  # Netherlands 3-4 Morocco
cur.execute("UPDATE matches SET final_result='H' WHERE match_id=537425")  # Mexico 2-0 Ecuador
cur.execute("UPDATE matches SET final_result='H' WHERE match_id=537421")  # USA 2-0 Bosnia-H.
# 2. 校正 USA vs Belgium: 填真实赛果到 537380, 删幻影 2130601
cur.execute("UPDATE matches SET status='finished', final_result='A', home_score=1, away_score=3 WHERE match_id=537380")
cur.execute("DELETE FROM matches WHERE match_id=2130601")
con.commit()
print("[update] committed")

# ── 后置断言 ──
post = cur.execute("""SELECT match_id,home_team_name,away_team_name,match_date,home_score,away_score,
                              final_result,status FROM matches WHERE match_id IN (537418,537425,537421,537380)""").fetchall()
pm = {r['match_id']: r for r in post}
assert pm[537418]['final_result'] == 'A', "537418 应为 A"
assert pm[537425]['final_result'] == 'H', "537425 应为 H"
assert pm[537421]['final_result'] == 'H', "537421 应为 H"
assert pm[537380]['status'] == 'finished' and pm[537380]['final_result'] == 'A' and pm[537380]['home_score']==1 and pm[537380]['away_score']==3, "537380 赛果应填"
gone = cur.execute("SELECT COUNT(*) c FROM matches WHERE match_id=2130601").fetchone()['c']
assert gone == 0, "2130601 应已删除"
# 不应再有 finished 但 final_result=None
bad = cur.execute("SELECT COUNT(*) c FROM matches WHERE league_name='世界杯' AND status='finished' AND final_result IS NULL").fetchone()['c']
assert bad == 0, f"不应有 finished 但 fr=None 的行, 实际 {bad}"
# WC 行数: 136 -> 135
n_wc = cur.execute("SELECT COUNT(*) c FROM matches WHERE league_name='世界杯'").fetchone()['c']
assert n_wc == 135, f"WC 行数应=135, 实际 {n_wc}"
print(f"[post-check] OK | WC行数={n_wc} | 已删幻影=2130601")

# ── 只读检测: 其他'中文源早1天'重复对(本次不删, 待用户批准) ──
print("\n=== 只读检测: 其他'中文源/英文源 日期差1天'重复对 ===")
rows = cur.execute("SELECT match_id,match_date,home_team_name,away_team_name,status,final_result FROM matches WHERE league_name='世界杯'").fetchall()
import collections
g = collections.defaultdict(list)
for r in rows:
    d = datetime.datetime.strptime(r['match_date'], "%Y-%m-%d").date()
    g[(r['home_team_name'], r['away_team_name'])].append((r['match_id'], d, r['status'], r['final_result']))
dups = []
for (h,a), lst in g.items():
    if len(lst) > 1:
        lst.sort(key=lambda x: x[1])
        d0, d1 = lst[0][1], lst[1][1]
        gap = (d1 - d0).days
        if gap == 1:  # 中文源早1天模式
            dups.append((h,a,lst))
print(f"发现 {len(dups)} 对'日期差1天'重复(含刚处理的 USA/Belgium):")
for h,a,lst in dups:
    print(f"  {h} vs {a}: " + ", ".join(f"{m}({d},{s},{fr})" for m,d,s,fr in lst))

con.close()
print(f"\n[done] 备份: {BACKUP}")
print("[note] 上述'日期差1天'重复对中, 除 USA/Belgium 外均未删除, 建议后续统一清理(保留英文有特征副本, 删中文源)。")
