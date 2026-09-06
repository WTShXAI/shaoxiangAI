"""
db_migrations — odds 表幂等写入 + 复合索引 + 增量备份 (E5 P1-14).

问题 (专家评审):
  - odds/odds_history/odds_timeline 缺 UPSERT → 重跑重复行.
  - 跨书 pivot 无复合索引.
  - data/ 下 10 个整库 _backup_*.db 共 3.9GB, 整库复制非增量.

修复 (全部 IF NOT EXISTS / 幂等, 可重复安全执行):
  1. schema_migrations 版本表 — 记录已应用迁移, 避免重复执行.
  2. 各 odds 表: 先去重 (保留 rowid 最小), 再建 UNIQUE 索引 → INSERT OR REPLACE 幂等.
  3. 复合 pivot 索引 (provider, ts) 加速跨书对齐.
  4. vacuum_into_backup(): VACUUM INTO 时间戳版本备份 (替代整库目录拷贝).

注意: 去重会修改生产 DB 行; 执行前务必先备份. 本脚本在临时 DB 上通过测试
(tests/test_db_migrations.py), 生产执行需维护窗口.
"""
import os
import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger("db_migrations")

MIGRATION_VERSION = 1

# 表 -> 去重/唯一键 (按真实列; 这些表仅 1X2, 无 market/line)
UNIQUE_KEYS = {
    "odds": ["match_id", "provider", "odds_timestamp"],
    "odds_history": ["match_id", "provider", "odds_timestamp"],
    "odds_timeline": ["match_id", "bookmaker", "snapshot_time"],
}
# 复合 pivot 索引: (provider/bookmaker, ts) 加速跨书对齐
PIVOT_INDEXES = {
    "odds": ("idx_odds_pivot", ["provider", "odds_timestamp"]),
    "odds_history": ("idx_odds_history_pivot", ["provider", "odds_timestamp"]),
    "odds_timeline": ("idx_odds_timeline_pivot", ["bookmaker", "snapshot_time"]),
}


def _ensure_migrations_table(con: sqlite3.Connection):
    con.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )"""
    )


def _is_applied(con: sqlite3.Connection, version: int) -> bool:
    row = con.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone()
    return row is not None


def _dedup_and_unique(con: sqlite3.Connection, table: str, keys: list):
    """去重(保留 rowid 最小)并建立 UNIQUE 索引, 使写入幂等."""
    key_cols = ", ".join(keys)
    # 1) 删除重复行, 仅在确有重复时执行
    dup_sql = f"""
        DELETE FROM {table}
        WHERE rowid NOT IN (
            SELECT MIN(rowid) FROM {table} GROUP BY {key_cols}
        )
    """
    before = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.execute(dup_sql)
    after = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    removed = before - after
    if removed:
        logger.info("[db_migrations] %s 去重移除 %d 行", table, removed)
    # 2) 建 UNIQUE 索引 (幂等)
    idx_name = f"uq_{table}_{'_'.join(keys)}"
    con.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {idx_name} ON {table} ({key_cols})"
    )
    return removed


def apply_migrations(db_path: str, force: bool = False) -> dict:
    """对指定 DB 应用 odds 幂等迁移. 返回执行摘要.

    调用方应使用 INSERT OR REPLACE 写入, 以利用 UNIQUE 索引实现幂等.
    """
    con = sqlite3.connect(db_path)
    try:
        _ensure_migrations_table(con)
        if _is_applied(con, MIGRATION_VERSION) and not force:
            logger.info("[db_migrations] v%d 已应用, 跳过", MIGRATION_VERSION)
            return {"applied": False, "version": MIGRATION_VERSION}
        summary = {"applied": True, "version": MIGRATION_VERSION, "dedup": {}, "pivot_indexes": []}
        for table, keys in UNIQUE_KEYS.items():
            # 表可能不存在(如新环境) → 跳过
            if not con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                logger.warning("[db_migrations] 表 %s 不存在, 跳过", table)
                continue
            removed = _dedup_and_unique(con, table, keys)
            summary["dedup"][table] = removed
            if table in PIVOT_INDEXES:
                idx_name, cols = PIVOT_INDEXES[table]
                con.execute(
                    f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({', '.join(cols)})"
                )
                summary["pivot_indexes"].append(idx_name)
        con.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (MIGRATION_VERSION, datetime.utcnow().isoformat()),
        )
        con.commit()
        logger.info("[db_migrations] v%d 应用完成", MIGRATION_VERSION)
        return summary
    finally:
        con.close()


def vacuum_into_backup(db_path: str, backup_dir: str) -> str:
    """增量(版本化)备份: VACUUM INTO 时间戳文件, 替代整库目录拷贝."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.splitext(os.path.basename(db_path))[0]
    backup_path = os.path.join(backup_dir, f"{base}_vacuum_{stamp}.db")
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"VACUUM INTO '{backup_path}'")
    finally:
        con.close()
    logger.info("[db_migrations] VACUUM 备份 -> %s", backup_path)
    return backup_path


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else "data/football_data.db"
    print(apply_migrations(target))
