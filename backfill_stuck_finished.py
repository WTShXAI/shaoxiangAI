"""一次性回填: 把 status='scheduled' 但 kickoff 已超 2.25h 的孤儿比赛翻成 finished.

根因: 旧 _sweep_finished 只扫 status='live', scheduled 老比赛(开赛后 GQ 列表下架, 采集器
不再触碰)永不进归档 → 堆积 456 场 week-old 比赛卡在 scheduled(前端当"未开赛"显示, 实为已结束).
auto_collector.py 的 _sweep_finished 现已加 scheduled→finished 分支, 本脚本把历史存量一次性清掉.

铁律: 不伪造比分. 沿用既有 score_home/away(多为 NULL), 仅改 status/minute/last_seen.
尊重 is_override: 人工锁定的比赛绝不覆盖.

用法:
  python backfill_stuck_finished.py          # dry-run, 只统计+抽样
  python backfill_stuck_finished.py --apply  # 真正写库
"""
import sqlite3, sys, time
from datetime import datetime, timezone, timedelta

DB = "D:/Architecture/data/events.db"
APPLY = "--apply" in sys.argv
CUTOFF = 2.25 * 3600  # 开赛超此时长仍 scheduled → 真实已结束

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
now_s = time.time()

rows = con.execute(
    "SELECT match_key, home, away, league, kickoff, score_home, score_away, is_override "
    "FROM matches WHERE status='scheduled' AND kickoff IS NOT NULL AND kickoff != ''"
).fetchall()

candidates = []      # age>2.25h, 可翻 finished
future_or_recent = 0 # age<=2.25h(含未来): 不动
skipped_override = 0 # is_override 锁定: 跳过
parse_fail = 0

for r in rows:
    try:
        kt = datetime.strptime(r["kickoff"][:16], "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone(timedelta(hours=8)))
        age = now_s - kt.timestamp()
    except Exception:
        parse_fail += 1
        continue
    if age <= CUTOFF:
        future_or_recent += 1
        continue
    if r["is_override"]:
        skipped_override += 1
        continue
    candidates.append(r)

print(f"[dry-run={not APPLY}] 扫描 scheduled 且有 kickoff: {len(rows)} 场")
print(f"  可翻 finished (age>{CUTOFF/3600}h): {len(candidates)}")
print(f"  不动 (age<={CUTOFF/3600}h, 含未来): {future_or_recent}")
print(f"  跳过 (is_override 锁定): {skipped_override}")
print(f"  kickoff 解析失败: {parse_fail}")
print("  抽样(前8):")
for r in candidates[:8]:
    kt = datetime.strptime(r["kickoff"][:16], "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone(timedelta(hours=8)))
    age_h = (now_s - kt.timestamp()) / 3600
    sc = f"{r['score_home']}-{r['score_away']}" if (r['score_home'] is not None) else "NULL"
    print(f"    {r['match_key'][:40]:40} ko={r['kickoff']} age={age_h:.1f}h score={sc}")

if not APPLY:
    print("\n[DRY-RUN] 未写库. 加 --apply 执行.")
    con.close()
    sys.exit(0)

# ── APPLY ──
n = 0
for r in candidates:
    con.execute(
        "UPDATE matches SET status='finished', minute=90, last_seen=? "
        "WHERE match_key=? AND (is_override IS NULL OR is_override=0)",
        (now_s, r["match_key"]))
    n += 1
con.commit()
con.close()
print(f"\n[APPLY] 已翻 finished: {n} 场 (备份 events.db.bak_20260815_1430_stuckfix)")
