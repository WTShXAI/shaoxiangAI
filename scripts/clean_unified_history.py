"""清洗 unified_history.db 的脏行 (事故⑦, T09 + T13).

铁律:
  - 默认 dry-run: 只出清单 + 行数 + 主要脏型, **不写**。
  - 仅 ``--apply`` 才执行删除; 执行前自动备份, 可回滚(恢复备份 或 ``--rollback BACKUP``)。
  - 清洗**只针对脏行**, 严禁动真实赛果/赔率数据(不触碰 match_outcomes 盘口列)。
  - 双源 ROI 偏差标记由 ``pipeline/compute_value_layer.align_dual_source_roi`` 负责,
    本脚本只清脏。

脏型(判定标准, 与设计/护栏一致):
  - missing:match_id/market/timestamp 为 None 或空串
  - roi_not_numeric / roi_not_finite / roi_out_of_range (|roi| 超界)
  - bad_source (source 不在 {LEYU,LEISU,UNIFIED,DISPUTED})
  - nonpositive_odds (odds 存在且 <= 0)
  - dup_key (同 match+market+timestamp+source 重复主键)

用法:
  python scripts/clean_unified_history.py --db data/unified_history.db            # dry-run
  python scripts/clean_unified_history.py --db data/unified_history.db --apply   # 备份+删脏行
  python scripts/clean_unified_history.py --rollback data/backups/unified_history_xxx.db  # 回滚
"""
from __future__ import annotations

import argparse
import math
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "unified_history.db"
DEFAULT_TABLE = "unified_history"

#: 脏行 ROI 超界阈值 (pp); 远超合理 ROI 区间者视为占位/垃圾.
ROI_OUTLIER_PP = 1_000_000.0
KNOWN_SOURCES = {"LEYU", "LEISU", "UNIFIED", "DISPUTED"}


def _as_float(v: Any) -> Optional[float]:
    """安全转 float, 失败返回 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def classify_dirty(row: Dict[str, Any]) -> List[str]:
    """返回该行的脏型列表 (空 = 干净)。"""
    problems: List[str] = []
    for k in ("match_id", "market", "timestamp"):
        v = row.get(k)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            problems.append(f"missing:{k}")
    roi = _as_float(row.get("roi"))
    if roi is None:
        problems.append("roi_not_numeric")
    elif not math.isfinite(roi):
        problems.append("roi_not_finite")
    elif abs(roi) > ROI_OUTLIER_PP:
        problems.append("roi_out_of_range")
    src = row.get("source")
    if not src or str(src) not in KNOWN_SOURCES:
        problems.append("bad_source")
    odds = _as_float(row.get("odds"))
    if odds is not None and odds <= 0:
        problems.append("nonpositive_odds")
    return problems


def scan_dirty(
    conn: sqlite3.Connection, table: str
) -> Tuple[int, int, Dict[str, int], List[Dict[str, Any]], List[Tuple[int, str]]]:
    """扫描脏行。

    Returns:
        (total, dirty_count, type_counts, sample_rows, dirty_rowids_with_label)
    """
    total = 0
    dirty_count = 0
    type_counts: Dict[str, int] = {}
    sample: List[Dict[str, Any]] = []
    dirty_rows: List[Tuple[int, str]] = []
    seen_keys: Dict[Tuple[Any, Any, Any, Any], int] = {}
    cur = conn.execute(f"SELECT rowid, * FROM {table}")
    cols = [d[0] for d in cur.description]
    for r in cur:
        rec = dict(zip(cols, r))
        total += 1
        rowid = rec.get("rowid")
        problems = classify_dirty(rec)
        # 重复主键检测 (附加脏型, 不计入删除计数以免误删整组)
        key = (rec.get("match_id"), rec.get("market"), rec.get("timestamp"), rec.get("source"))
        if key in seen_keys:
            problems.append("dup_key")
        else:
            seen_keys[key] = rowid  # type: ignore[assignment]
        if problems:
            dirty_count += 1
            label = ",".join(problems)
            type_counts[label] = type_counts.get(label, 0) + 1
            dirty_rows.append((rowid, label))
            if len(sample) < 50:
                sample.append({
                    "rowid": rowid,
                    "problems": problems,
                    "match_id": rec.get("match_id"),
                    "market": rec.get("market"),
                    "timestamp": rec.get("timestamp"),
                    "source": rec.get("source"),
                    "roi": rec.get("roi"),
                })
    return total, dirty_count, type_counts, sample, dirty_rows


def _backup_path(db_path: Path, backup_dir: Optional[Path]) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if backup_dir is None:
        backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    return backup_dir / f"{db_path.stem}_{ts}.db"


def backup_db(db_path: Path, backup_dir: Optional[Path]) -> Path:
    """复制库到带时间戳的备份, 返回备份路径 (回滚用)。"""
    dst = _backup_path(db_path, backup_dir)
    shutil.copy2(str(db_path), str(dst))
    return dst


def restore_db(db_path: Path, backup: Path) -> None:
    """从备份恢复 (回滚)。"""
    shutil.copy2(str(backup), str(db_path))


def cmd_dryrun(conn: sqlite3.Connection, table: str) -> int:
    total, dirty, type_counts, sample, _ = scan_dirty(conn, table)
    print(f"[DRY-RUN] 表={table} 总行={total} 脏行={dirty} (默认不写)")
    if type_counts:
        print("  脏型分布:")
        for label, cnt in sorted(type_counts.items(), key=lambda kv: -kv[1]):
            print(f"    {label}: {cnt}")
    if sample:
        print(f"  样本(最多{len(sample)}行):")
        for s in sample[:10]:
            print(f"    rowid={s['rowid']} problems={s['problems']} "
                  f"match={s['match_id']} market={s['market']} ts={s['timestamp']} "
                  f"src={s['source']} roi={s['roi']}")
    print("[DRY-RUN] 如需执行清理: 加 --apply (将先自动备份, 可 --rollback 回滚)")
    return 0


def cmd_apply(conn: sqlite3.Connection, table: str, db_path: Path,
              backup_dir: Optional[Path], keep_quarantine: bool) -> int:
    total, dirty, type_counts, sample, dirty_rows = scan_dirty(conn, table)
    if dirty == 0:
        print(f"[APPLY] 无脏行, 无需清理 (表={table}, 总行={total})")
        return 0
    backup = backup_db(db_path, backup_dir)
    print(f"[APPLY] 已备份 -> {backup}")
    qtable = f"_clean_quarantine_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        # 隔离表: 结构与目标表一致 + _dirty_reason, 供回滚/审计
        cur.execute(f"CREATE TABLE IF NOT EXISTS {qtable} AS SELECT * FROM {table} WHERE 0=1")
        try:
            cur.execute(f"ALTER TABLE {qtable} ADD COLUMN _dirty_reason TEXT")
        except sqlite3.OperationalError:
            pass  # 已存在
        deleted = 0
        for rowid, label in dirty_rows:
            cur.execute(
                f"INSERT INTO {qtable} SELECT *, ? FROM {table} WHERE rowid=?",
                (label, rowid),
            )
            cur.execute(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
            deleted += 1
        if not keep_quarantine:
            cur.execute(f"DROP TABLE IF EXISTS {qtable}")
        conn.commit()
        print(f"[APPLY] 已删除脏行 {deleted} (表={table}); "
              f"{'隔离表=' + qtable if keep_quarantine else '未保留隔离表'}")
        print(f"[APPLY] 回滚: 恢复备份 {backup} 或 从隔离表 {qtable} 重插入")
        return 0
    except Exception as e:  # 异常全回滚, 不残留半截删除
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[APPLY] 失败已回滚: {e}", file=sys.stderr)
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="清洗 unified_history.db 脏行 (事故⑦)")
    p.add_argument("--db", default=str(DEFAULT_DB), help="unified_history.db 路径")
    p.add_argument("--table", default=DEFAULT_TABLE, help="目标表名")
    p.add_argument("--apply", action="store_true", help="执行清理(默认 dry-run)")
    p.add_argument("--backup-dir", default=None, help="备份目录")
    p.add_argument("--no-quarantine", action="store_true", help="不保留隔离表")
    p.add_argument("--rollback", default=None, help="从指定备份恢复(db 路径)")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if args.rollback:
        restore_db(db_path, Path(args.rollback))
        print(f"[ROLLBACK] 已从 {args.rollback} 恢复 -> {db_path}")
        return 0
    if not db_path.exists():
        print(f"[ERROR] 数据库不存在: {db_path} "
              f"(请确认路径; 本脚本不创建/不初始化业务库)", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (args.table,),
        ).fetchone()
        if tbl is None:
            print(f"[ERROR] 表 {args.table} 不存在于 {db_path}", file=sys.stderr)
            return 2
        if args.apply:
            return cmd_apply(
                conn, args.table, db_path,
                Path(args.backup_dir) if args.backup_dir else None,
                not args.no_quarantine,
            )
        return cmd_dryrun(conn, args.table)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
