r"""p_audit_migrate.py — P-AUDIT odds_changes 审计链前向迁移(非应用, 须维护窗口)

蓝图 §1 / P-AUDIT(docs/WINDOW_PREP.md §2): odds_changes(44.6M行) 缺 raw_json_hash + cleaned_id
(原 JSON 哈希 / 清洗 ID)。本脚本幂等 ALTER 补两列 + 索引。

安全设计:
- 默认 dry-run(只打印计划, 零修改)。
- 须 --apply 才执行; 对生产 events.db 额外须 --allow-production
  (35GB 库须停机窗口 + 回归, 禁在线盲跑)。
- 前向语义: 采集器不保留原始 JSON → 历史行 raw_json_hash/cleaned_id 恒 NULL,
  仅未来采集行由采集器填充。审计查询须 WHERE raw_json_hash IS NOT NULL。

用法:
  python scripts/p_audit_migrate.py --dry-run
  python scripts/p_audit_migrate.py --apply --db <非生产库>
  python scripts/p_audit_migrate.py --apply --allow-production   # 仅维护窗口, 对 events.db
"""
import argparse
import sqlite3
import sys

ADD_COLS = [
    ("raw_json_hash", "TEXT"),
    ("cleaned_id", "TEXT"),
]


def plan(db_path):
    """返回 odds_changes 尚缺的列 [(col, type), ...]。只读。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute("PRAGMA table_info(odds_changes)")
    existing = {r[1] for r in cur.fetchall()}
    con.close()
    return [(c, t) for c, t in ADD_COLS if c not in existing]


def migrate(db_path, apply, allow_production):
    """执行/打印 odds_changes 审计列迁移。返回 True=已应用或无需迁移。"""
    is_prod = "events.db" in db_path.replace("\\", "/")
    if is_prod and not allow_production:
        print(f"[REFUSE] {db_path} 是生产库, 须停机窗口 + --allow-production 才允许 --apply")
        return False
    if not apply:
        todo = plan(db_path)
        print(f"[DRY-RUN] 将对 {db_path} 的 odds_changes 执行:")
        for c, t in todo:
            print(f"  ALTER TABLE odds_changes ADD COLUMN {c} {t};")
        print("  CREATE INDEX IF NOT EXISTS ix_oc_raw_json_hash ON odds_changes(raw_json_hash);")
        print("[DRY-RUN] 未做任何修改。加 --apply 执行(生产库还需 --allow-production)。")
        return False
    todo = plan(db_path)
    if not todo:
        print("[OK] odds_changes 已含全部目标列, 无需迁移")
        return True
    con = sqlite3.connect(db_path)
    for c, t in todo:
        con.execute(f"ALTER TABLE odds_changes ADD COLUMN {c} {t}")
    con.execute("CREATE INDEX IF NOT EXISTS ix_oc_raw_json_hash ON odds_changes(raw_json_hash)")
    con.commit()
    con.close()
    print(f"[APPLIED] odds_changes 已补列: {[c for c, _ in todo]}; 索引 ix_oc_raw_json_hash 已建")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="D:/Architecture/data/events.db")
    ap.add_argument("--apply", action="store_true", help="执行迁移(默认 dry-run)")
    ap.add_argument("--allow-production", action="store_true",
                    help="允许对生产 events.db 执行(仍须 --apply, 且须停机窗口)")
    ap.add_argument("--dry-run", action="store_true", help="显式 dry-run(默认行为)")
    args = ap.parse_args()
    migrate(args.db, apply=args.apply, allow_production=args.allow_production)


if __name__ == "__main__":
    main()
