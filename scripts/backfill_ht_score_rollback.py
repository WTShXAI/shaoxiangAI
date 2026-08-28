"""回滚 backfill_ht_score.py 的 --apply 结果.

读取 _ht_backfill_rollback.csv, 把 matches.ht_score_home/away 置回 NULL
(仅回滚那些被本脚本补全的场次, 不动其他数据).

用法:
  python scripts/backfill_ht_score_rollback.py --csv scripts/_ht_backfill_rollback.csv
"""
import argparse
import csv
import sqlite3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--apply", action="store_true", help="默认 dry-run")
    args = ap.parse_args()

    rows = []
    with open(args.csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((r["match_key"],))

    c = sqlite3.connect("D:/Architecture/data/events.db", timeout=30)
    print(f"[dry-run] 将置回 NULL 的场次: {len(rows)}")
    if not args.apply:
        print("[dry-run] 加 --apply 执行.")
        c.close()
        return
    cur = c.executemany(
        "UPDATE matches SET ht_score_home=NULL, ht_score_away=NULL WHERE match_key=?",
        rows)
    c.commit()
    print(f"[apply] 已回滚 {cur.rowcount} 场 matches.ht_score 为 NULL")
    c.close()


if __name__ == "__main__":
    main()
