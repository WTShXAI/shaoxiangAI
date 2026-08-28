# -*- coding: utf-8 -*-
"""采集器存活探针 (供 docker-compose healthcheck 或 Windows 守护脚本复用).

判定逻辑: 采集器 gq/auto_collector.py 唯一外部信号是持续写 events.db。
若 events.db (含 -wal/-shm) 在阈值时间内无更新, 视为采集器假死。

用法:
  python deploy/collector_healthcheck.py [--db /app/data/events.db] [--max-minutes 10]

退出码: 0 = 存活, 1 = 假死/缺失。
仅用 ASCII 输出 (规避 Windows GBK 日志崩溃铁律)。
"""
import os
import sys
import time


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    db_path = "/app/data/events.db"
    max_minutes = 10
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--db":
            db_path = argv[i + 1]; i += 2
        elif a == "--max-minutes":
            max_minutes = int(argv[i + 1]); i += 2
        else:
            i += 1

    candidates = [db_path, db_path + "-wal", db_path + "-shm"]
    found = [p for p in candidates if os.path.exists(p)]
    if not found:
        print("[HEALTH] events.db not found at %s" % db_path)
        return 1

    now = time.time()
    newest = max(os.path.getmtime(p) for p in found)
    age_min = (now - newest) / 60.0
    if age_min <= max_minutes:
        print("[HEALTH] OK mtime_age_min=%.1f threshold=%d" % (age_min, max_minutes))
        return 0
    print("[HEALTH] STALE mtime_age_min=%.1f exceeds threshold=%d" %
          (age_min, max_minutes))
    return 1


if __name__ == "__main__":
    sys.exit(main())
