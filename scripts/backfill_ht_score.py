"""一次性回填: 把 match_outcomes 已存的半场比分(HT)补回 matches.ht_score_*

根因: upsert_match 在完场后每轮 re-sweep 无条件用 None 覆盖已采集的 HT 比分,
导致 matches.ht_score 覆盖率仅 30%. match_outcomes 因回填逻辑保留了 HT,
可作为可信来源反向补回 matches.

用法:
  python scripts/backfill_ht_score.py            # dry-run, 仅统计
  python scripts/backfill_ht_score.py --apply    # 执行 UPDATE
  python scripts/backfill_ht_score.py --apply --rollback-csv <path>  # 同时写回滚日志

审计: --apply 时把每条被更新的 (match_key, mid, ht_h, ht_a) 写入 CSV, 便于回滚.
回滚: 见 backfill_ht_score_rollback.py (用 CSV 把 matches.ht_score 置回 NULL).
"""

import argparse
import csv
import sqlite3
import sys

DB = "file:D:/Architecture/data/events.db?mode=ro" if False else "D:/Architecture/data/events.db"


def connect():
    return sqlite3.connect(DB, timeout=30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行 UPDATE (默认 dry-run)")
    ap.add_argument("--rollback-csv", default=None, help="--apply 时写回滚日志的路径")
    args = ap.parse_args()

    c = connect()
    cur = c.cursor()

    # 待回填: matches.ht 为空, 但 match_outcomes 同 mid 有 ht
    q = """
    SELECT m.match_key, m.mid, mo.ht_score_home, mo.ht_score_away
    FROM matches m
    JOIN match_outcomes mo ON mo.mid = m.mid
    WHERE m.ht_score_home IS NULL
      AND mo.ht_score_home IS NOT NULL
      AND mo.ht_score_away IS NOT NULL
    """
    rows = cur.execute(q).fetchall()
    print(f"[dry-run] 可回填场次: {len(rows)}")

    if not rows:
        c.close()
        return

    # 抽样展示
    print("  样例:")
    for r in rows[:8]:
        print(f"    {r[0]}  mid={r[1]}  HT {r[2]}-{r[3]}")

    if not args.apply:
        print("[dry-run] 未执行. 加 --apply 执行.")
        c.close()
        return

    # 执行 UPDATE
    upd = 0
    audit = []
    for match_key, mid, ht_h, ht_a in rows:
        cur.execute(
            "UPDATE matches SET ht_score_home=?, ht_score_away=? WHERE match_key=?",
            (ht_h, ht_a, match_key))
        upd += 1
        audit.append((match_key, mid, ht_h, ht_a))

    c.commit()
    print(f"[apply] 已回填 {upd} 场 matches.ht_score")

    if args.rollback_csv:
        with open(args.rollback_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["match_key", "mid", "ht_score_home", "ht_score_away"])
            for r in audit:
                w.writerow(r)
        print(f"[apply] 回滚日志已写: {args.rollback_csv} ({len(audit)} 行)")

    # 回填后覆盖率
    tot = cur.execute("SELECT COUNT(*) FROM matches WHERE status='finished'").fetchone()[0]
    ht = cur.execute("SELECT COUNT(*) FROM matches WHERE status='finished' AND ht_score_home IS NOT NULL").fetchone()[0]
    print(f"[apply] matches finished ht 覆盖率: {ht}/{tot} = {ht/tot*100:.1f}%")
    c.close()


if __name__ == "__main__":
    main()
