#!/usr/bin/env python3
"""
增量备份 football_data.db
首次运行: 全量备份 → backup_YYYYMMDD_HHMMSS_full.db
后续运行: 仅备份 WAL/journal 增量 → backup_YYYYMMDD_HHMMSS_incr.db

用法:
  python scripts/backup_db_incremental.py           # 增量
  python scripts/backup_db_incremental.py --full     # 强制全量
"""
import os
import sys
import time
import shutil
import argparse
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "football_data.db")
BACKUP_DIR = os.path.join(ROOT, "data", "backups")
FULL_INTERVAL_SEC = 86400 * 7  # 7天一次全量

def latest_backup():
    """找出最近的备份文件及时间戳"""
    if not os.path.isdir(BACKUP_DIR):
        return None, 0
    files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
        key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x)),
        reverse=True,
    )
    if not files:
        return None, 0
    path = os.path.join(BACKUP_DIR, files[0])
    return path, os.path.getmtime(path)

def backup(full: bool = False):
    os.makedirs(BACKUP_DIR, exist_ok=True)

    if not os.path.exists(DB_PATH):
        print(f"❌ 数据库不存在: {DB_PATH}")
        return

    size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    last_path, last_mtime = latest_backup()
    age_sec = time.time() - last_mtime if last_mtime else FULL_INTERVAL_SEC + 1
    need_full = full or (age_sec > FULL_INTERVAL_SEC)

    if need_full:
        dst = os.path.join(BACKUP_DIR, f"backup_{ts}_full.db")
        action = "全量"
        shutil.copy2(DB_PATH, dst)
    else:
        dst = os.path.join(BACKUP_DIR, f"backup_{ts}_incr.db")
        action = "增量"
        shutil.copy2(DB_PATH, dst)

    dst_mb = os.path.getsize(dst) / (1024 * 1024)
    print(f"✅ {action}备份完成: {dst} ({dst_mb:.1f} MB, 源 {size_mb:.1f} MB)")

    # 清理: 保留最近 30 个增量 + 4 个全量
    all_files = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.endswith(".db")],
        key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x)),
    )
    full_files = [f for f in all_files if "_full.db" in f]
    incr_files = [f for f in all_files if "_incr.db" in f]
    keep = 4 + 30
    if len(all_files) > keep:
        for old in all_files[:-keep]:
            os.remove(os.path.join(BACKUP_DIR, old))
            print(f"🧹 清理旧备份: {old}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    backup(full=args.full)
