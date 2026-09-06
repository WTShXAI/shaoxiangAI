"""清理僵尸 live: status='live' 但比赛早已结束的场次 (IR-06 状态单调)。

根因: gq/auto_collector.py `_sweep_finished` 原判据只有 `age >= 3.5h` 单条件,
      刚结束的比赛要干等 3.5 小时才归档。实测莫斯科斯巴达U19 vs 罗迪那U19
      (kickoff 22:00, minute=116, 比分 3-1 已定) 在 01:21 时 age=3.35h, 差 9 分钟
      卡着不动, 前端一直当"进行中"展示。

本脚本用与 `_sweep_finished` **相同的判据**立即清理存量:
      age >= 2.5h 且 minute >= 90  →  finished
  (2.5h 覆盖加时+点球最长 ~135min; minute>=90 是 feed 补时真值, 可信)

铁律:
  - 不伪造比分: 仅改 status/minute/last_seen, 沿用既有 score_home/away
  - 尊重 is_override: 人工锁定的比赛绝不覆盖
  - 无比分 → 不归档赛果(留人工), 但仍清状态

用法:
  python scripts/sweep_zombie_live_20260829.py           # dry-run
  python scripts/sweep_zombie_live_20260829.py --apply   # 真正写库
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")
APPLY = "--apply" in sys.argv
EARLY_AGE = 2.5 * 3600      # 与 _sweep_finished 的早判阈值一致
FALLBACK_AGE = 3.5 * 3600   # 无 minute 信息时的兜底阈值

con = sqlite3.connect(DB, timeout=60)
con.row_factory = sqlite3.Row
now_s = time.time()

rows = con.execute("""
    SELECT match_key, home, away, league, kickoff, score_home, score_away,
           minute, mid, is_override, last_seen
    FROM matches WHERE status='live'
""").fetchall()

cands = []
for r in rows:
    try:
        kt = datetime.strptime(r["kickoff"][:16], "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone(timedelta(hours=8)))
        age = now_s - kt.timestamp()
    except Exception:
        continue
    if age < 0:
        continue                                   # 未来开赛 → 不是僵尸
    mn = r["minute"]
    mn = int(mn) if mn is not None else None
    if age >= FALLBACK_AGE:
        hit, why = True, f"age>={FALLBACK_AGE/3600}h 兜底"
    elif age >= EARLY_AGE and mn is not None and mn >= 90:
        hit, why = True, f"age>={EARLY_AGE/3600}h 且 minute={mn}>=90"
    else:
        hit, why = False, f"age={age/3600:.2f}h minute={mn} (未达阈值)"
    if not hit:
        continue
    if r["is_override"]:
        print(f"  [跳过·人工锁定] {r['match_key']}")
        continue
    cands.append((r, age, why))

print(f"[dry-run={not APPLY}] 扫描 status='live': {len(rows)} 场")
print(f"  判定为僵尸(应翻 finished): {len(cands)} 场")
for r, age, why in cands:
    sc = f"{r['score_home']}-{r['score_away']}" if r["score_home"] is not None else "NULL(不归档赛果)"
    print(f"    {r['match_key'][:38]:38s} ko={r['kickoff']} age={age/3600:.2f}h "
          f"minute={r['minute']} score={sc}  [{why}]")

if not APPLY:
    print("\n[DRY-RUN] 未写库. 加 --apply 执行.")
    con.close()
    sys.exit(0)

n = 0
for r, age, why in cands:
    con.execute(
        "UPDATE matches SET status='finished', last_seen=? "
        "WHERE match_key=? AND (is_override IS NULL OR is_override=0)",
        (now_s, r["match_key"]))
    n += 1
    # 有终比分则归档初盘→赛果
    if r["mid"] and r["score_home"] is not None and r["score_away"] is not None:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from gq.db import record_match_outcome
            outcome = record_match_outcome(
                r["mid"], r["home"], r["away"], r["league"], kickoff=r["kickoff"],
                score_home=r["score_home"], score_away=r["score_away"],
                match_key_override=r["match_key"])
            if outcome:
                print(f"    归档赛果: {r['match_key']} [{outcome.get('result')}]")
        except Exception as e:
            print(f"    [归档失败·不影响状态清理] {r['match_key']}: {e}")
con.commit()
con.close()
print(f"\n[APPLY] 已翻 finished: {n} 场")
