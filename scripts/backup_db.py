"""
数据库定时备份脚本
=====================
每天备份 SQLite 数据库，保留最近 7 份备份。

用法:
    python scripts/backup_db.py              # 手动运行备份
    python scripts/backup_db.py --prune-only  # 仅清理过期备份

定时任务 (crontab / 任务计划程序):
    # 每天凌晨 3:00 执行备份
    0 3 * * * cd /path/to/project && python scripts/backup_db.py
"""
import os
import sys
import shutil
import argparse
import logging
from datetime import datetime, timezone, timedelta

# ── 确保项目根目录可导入 ──────────────────
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("backup_db")

# ── 配置 ──────────────────────────────────
BACKUP_DIR = os.path.join(_project_root, "data", "backups")
RETENTION_DAYS = 7

# 数据库路径 (优先从 settings 读取, 回退默认)
try:
    from core.config import settings
    DB_SOURCE = settings.DB_PATH
    if not os.path.isabs(DB_SOURCE):
        DB_SOURCE = os.path.join(settings.PROJECT_ROOT, DB_SOURCE)
    DATABASE_URL = settings.DATABASE_URL
except (ImportError, Exception) as e:
    logger.warning(f"无法加载 settings: {e}，使用默认路径")
    DB_SOURCE = os.path.join(_project_root, "data", "football_data.db")
    DATABASE_URL = f"sqlite:///{DB_SOURCE.replace(os.sep, '/')}"

def ensure_backup_dir():
    """确保备份目录存在"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    logger.info(f"备份目录: {BACKUP_DIR}")

def do_backup() -> str | None:
    """执行备份, 返回备份文件路径"""
    if not os.path.exists(DB_SOURCE):
        logger.error(f"数据库文件不存在: {DB_SOURCE}")
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # 同时备份 WAL 和 SHM 文件（如果存在）
    db_name = os.path.basename(DB_SOURCE)
    backup_name = f"{os.path.splitext(db_name)[0]}_{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    try:
        # 先执行 checkpoint 确保数据一致性
        try:
            from sqlalchemy import create_engine, text
            _eng = create_engine(DATABASE_URL)
            with _eng.connect() as _conn:
                _conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                _conn.commit()
            _eng.dispose()
        except Exception as checkpoint_err:
            logger.warning(f"WAL checkpoint 失败 (不影响备份): {checkpoint_err}")

        shutil.copy2(DB_SOURCE, backup_path)
        logger.info(f"数据库备份完成: {backup_path} ({os.path.getsize(backup_path) / 1024:.1f} KB)")

        # 可选: 备份 WAL 文件
        wal_path = DB_SOURCE + "-wal"
        if os.path.exists(wal_path):
            shutil.copy2(wal_path, backup_path + "-wal")
        return backup_path
    except Exception as e:
        logger.error(f"备份失败: {e}")
        return None

def prune_old_backups():
    """清理超过 RETENTION_DAYS 的旧备份"""
    if not os.path.exists(BACKUP_DIR):
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    removed = 0
    for fname in os.listdir(BACKUP_DIR):
        fpath = os.path.join(BACKUP_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(fpath))
        if mtime < cutoff:
            os.remove(fpath)
            removed += 1
            logger.info(f"删除过期备份: {fname}")

    if removed:
        logger.info(f"已清理 {removed} 个过期备份文件")
    else:
        logger.info("无过期备份需要清理")

def main():
    parser = argparse.ArgumentParser(description="SQLite 数据库备份工具")
    parser.add_argument("--prune-only", action="store_true", help="仅清理过期备份")
    args = parser.parse_args()

    ensure_backup_dir()

    if args.prune_only:
        prune_old_backups()
        return

    # 执行备份
    logger.info(f"开始备份数据库: {DB_SOURCE}")
    result = do_backup()
    if result:
        prune_old_backups()
        logger.info("备份完成")
    else:
        logger.error("备份失败，退出码 1")
        sys.exit(1)

if __name__ == "__main__":
    main()
