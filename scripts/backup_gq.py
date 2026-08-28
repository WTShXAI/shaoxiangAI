#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""events.db 备份 / 坏页恢复 SOP 脚本 (REQ-12, T12).

做的事 (按顺序):
    1. ``PRAGMA wal_checkpoint(TRUNCATE)`` —— 把 ``-wal`` 内容落回主库, 保证快照自洽;
    2. sqlite **在线备份 API** 生成快照 (不锁读, 比 ``copy`` 安全);
    3. 对快照跑 ``PRAGMA integrity_check`` 校验; 失败即告警并以非零码退出;
    4. 对**源库**跑 integrity_check; 发现坏页 → 告警 (可选 ``--auto-restore`` 从最近
       可用快照自动恢复);
    5. 按 ``--keep`` 清理过期快照。

用法::

    python scripts/backup_gq.py                        # 备份到 <db_dir>/backups/
    python scripts/backup_gq.py --dest D:/bak/events.db    # 备份到指定文件
    python scripts/backup_gq.py --dest D:/bak          # dest 是目录 → 自动生成带时间戳文件名
    python scripts/backup_gq.py --check-only           # 只做坏页自检, 不备份
    python scripts/backup_gq.py --auto-restore         # 源库坏页时自动从最近快照恢复
    python scripts/backup_gq.py --keep 14              # 只保留最近 14 份快照

退出码: 0 = 全部成功; 1 = 备份/校验失败或发现坏页未恢复; 2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

# 允许以 `python scripts/backup_gq.py` 直接运行 (把项目根加入 sys.path)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.config import get_config  # noqa: E402
from core.db_manager import (  # noqa: E402
    DBManagerError,
    GQConnectionManager,
    IntegrityError,
    _verify_file_integrity,
)
from core.logging_config import configure_logging, get_logger, trace_context  # noqa: E402
from core.safe_log import install_utf8, safe_print, safe_str  # noqa: E402

#: 快照文件名时间戳格式。
TS_FORMAT = "%Y%m%d-%H%M%S"

#: 默认保留的快照份数。
DEFAULT_KEEP = 7

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="backup_gq.py",
        description="events.db 备份 + WAL checkpoint + 坏页自检 (REQ-12)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="源数据库路径 (默认取 core.config.Config.gq_db_path)",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="备份目标: 文件路径或目录 (默认 <db_dir>/backups/)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"保留最近 N 份快照, 0=不清理 (默认 {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只做 integrity_check, 不生成备份",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="跳过 wal_checkpoint(TRUNCATE)",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="源库自检用 quick_check 代替 integrity_check (大库更快)",
    )
    parser.add_argument(
        "--auto-restore",
        action="store_true",
        help="源库坏页时, 自动从最近一个通过校验的快照恢复",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出 DEBUG 级日志",
    )
    return parser


def resolve_dest(dest: Optional[str], db_path: str) -> Path:
    """决定备份文件的最终路径。

    Args:
        dest: 用户给的 ``--dest``; 可为 None / 目录 / 文件路径。
        db_path: 源库路径 (用于推导默认目录与文件名)。

    Returns:
        备份文件的绝对路径 (父目录可能尚未创建)。
    """
    db_file = Path(db_path)
    stamp = time.strftime(TS_FORMAT)
    default_name = f"{db_file.stem or 'GQ'}-{stamp}.db"

    if not dest:
        return (db_file.parent / "backups" / default_name).resolve()

    candidate = Path(dest).expanduser()
    # 目录判定: 已存在的目录, 或以分隔符结尾, 或没有后缀
    looks_like_dir = (
        candidate.is_dir()
        or dest.endswith(("/", "\\"))
        or (not candidate.suffix and not candidate.is_file())
    )
    if looks_like_dir:
        return (candidate / default_name).resolve()
    return candidate.resolve()


def list_snapshots(directory: Path, db_stem: str) -> List[Path]:
    """列出目录内属于该库的快照, 按修改时间从新到旧排序。

    Args:
        directory: 快照目录。
        db_stem: 源库文件名主干 (如 ``GQ``)。

    Returns:
        快照路径列表 (新 → 旧)。
    """
    if not directory.is_dir():
        return []
    pattern = str(directory / f"{db_stem}-*.db")
    files = [Path(p) for p in glob.glob(pattern) if os.path.isfile(p)]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def prune_snapshots(directory: Path, db_stem: str, keep: int, logger) -> int:
    """删除超出保留份数的旧快照。

    Args:
        directory: 快照目录。
        db_stem: 源库文件名主干。
        keep: 保留份数; ``<=0`` 表示不清理。
        logger: 结构化 logger。

    Returns:
        实际删除的文件数。
    """
    if keep <= 0:
        return 0
    snapshots = list_snapshots(directory, db_stem)
    removed = 0
    for stale in snapshots[keep:]:
        try:
            stale.unlink()
            removed += 1
            logger.info("pruned stale snapshot", path=str(stale))
        except Exception as exc:
            logger.warning("prune failed", path=str(stale), detail=safe_str(exc, 300))
    return removed


def find_healthy_snapshot(directory: Path, db_stem: str, busy_timeout_ms: int) -> Optional[Path]:
    """找到最近一个通过完整性校验的快照 (用于坏页自愈)。

    Args:
        directory: 快照目录。
        db_stem: 源库文件名主干。
        busy_timeout_ms: busy_timeout。

    Returns:
        快照路径, 或 ``None``。
    """
    for candidate in list_snapshots(directory, db_stem):
        if _verify_file_integrity(str(candidate), busy_timeout_ms):
            return candidate
    return None


def run(argv: Optional[List[str]] = None) -> int:
    """脚本主流程。

    Args:
        argv: 命令行参数 (默认 ``sys.argv[1:]``)。

    Returns:
        进程退出码。
    """
    install_utf8()
    args = build_parser().parse_args(argv)

    cfg = get_config()
    configure_logging(level="DEBUG" if args.verbose else cfg.log_level)
    logger = get_logger("scripts.backup_gq")

    db_path = str(args.db or cfg.gq_db_path)
    if not os.path.isfile(db_path):
        logger.error("source database not found", db_path=db_path)
        safe_print(f"[FAIL] source database not found: {db_path}")
        return EXIT_USAGE

    manager = GQConnectionManager(
        db_path=db_path,
        busy_timeout_ms=cfg.busy_timeout_ms,
        logger=logger,
    )

    exit_code = EXIT_OK
    with trace_context("backup") as trace_id:
        logger.info("backup job started", db_path=db_path, trace_id=trace_id)
        try:
            manager.init_db()
        except DBManagerError as exc:
            logger.error("init_db failed", detail=safe_str(exc, 500))
            safe_print(f"[FAIL] init_db: {safe_str(exc, 500)}")
            return EXIT_FAIL

        # ---- 1) WAL checkpoint --------------------------------------
        if not args.no_checkpoint:
            if manager.checkpoint("TRUNCATE"):
                logger.info("wal_checkpoint(TRUNCATE) ok")
                safe_print("[OK]   wal_checkpoint(TRUNCATE)")
            else:
                # busy 不算致命: 备份 API 仍能拿到一致快照
                logger.warning("wal_checkpoint busy or failed (continuing)")
                safe_print("[WARN] wal_checkpoint busy/failed (continuing)")

        # ---- 2) 源库坏页自检 ----------------------------------------
        source_healthy = manager.check_integrity(quick=bool(args.quick))
        if source_healthy:
            logger.info("source integrity ok", quick=bool(args.quick))
            safe_print("[OK]   source integrity_check = ok")
        else:
            logger.error("source integrity FAILED (corrupt page suspected)", db_path=db_path)
            safe_print("[ALERT] source integrity_check FAILED — corrupt page suspected!")
            exit_code = EXIT_FAIL

        # ---- 3) 备份 ------------------------------------------------
        backup_path: Optional[Path] = None
        if args.check_only:
            safe_print("[SKIP] --check-only: backup not performed")
        elif not source_healthy:
            # 源库已坏, 不能用坏库覆盖好快照
            safe_print("[SKIP] source is corrupt — refusing to overwrite good snapshots")
            logger.error("backup skipped because source is corrupt")
        else:
            dest = resolve_dest(args.dest, db_path)
            try:
                written = manager.backup(str(dest), verify=True)
                backup_path = Path(written)
                size_mb = backup_path.stat().st_size / (1024 * 1024)
                logger.info("backup ok", dest=written, size_mb=round(size_mb, 2))
                safe_print(f"[OK]   backup -> {written} ({size_mb:.2f} MB, integrity verified)")
            except IntegrityError as exc:
                logger.error("backup verification failed", detail=safe_str(exc, 500))
                safe_print(f"[FAIL] backup verification failed: {safe_str(exc, 300)}")
                exit_code = EXIT_FAIL
            except DBManagerError as exc:
                logger.error("backup failed", detail=safe_str(exc, 500))
                safe_print(f"[FAIL] backup failed: {safe_str(exc, 300)}")
                exit_code = EXIT_FAIL

        # ---- 4) 坏页自愈 (可选) --------------------------------------
        if not source_healthy and args.auto_restore:
            directory = (
                backup_path.parent
                if backup_path is not None
                else resolve_dest(args.dest, db_path).parent
            )
            snapshot = find_healthy_snapshot(directory, Path(db_path).stem, cfg.busy_timeout_ms)
            if snapshot is None:
                logger.error("auto-restore aborted: no healthy snapshot", directory=str(directory))
                safe_print(f"[FAIL] auto-restore: no healthy snapshot in {directory}")
            else:
                try:
                    manager.restore(str(snapshot), verify=True)
                    if manager.check_integrity(quick=True):
                        logger.info("auto-restore succeeded", source=str(snapshot))
                        safe_print(f"[OK]   auto-restore from {snapshot}")
                        exit_code = EXIT_OK
                    else:
                        logger.error("restored db still fails integrity", source=str(snapshot))
                        safe_print("[FAIL] restored db still fails integrity_check")
                except (DBManagerError, IntegrityError) as exc:
                    logger.error("auto-restore failed", detail=safe_str(exc, 500))
                    safe_print(f"[FAIL] auto-restore failed: {safe_str(exc, 300)}")

        # ---- 5) 清理旧快照 ------------------------------------------
        if backup_path is not None and args.keep > 0:
            removed = prune_snapshots(backup_path.parent, Path(db_path).stem, args.keep, logger)
            if removed:
                safe_print(f"[OK]   pruned {removed} stale snapshot(s), keep={args.keep}")

        manager.close_all()
        logger.info("backup job finished", exit_code=exit_code)

    safe_print("[DONE] backup_gq exit_code=%d" % exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(run())
