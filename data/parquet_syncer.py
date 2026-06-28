"""
钱代驾 — SQLite → Parquet 离线训练集同步管道
==============================================
职责: 将 SQLite 核心表转存为列式 Parquet，加速大规模回测。

触发方式:
  - 手动: python data/parquet_syncer.py
  - 省电模式: python data/parquet_syncer.py --eco (低内存/分批写入)
  - 定时: 配合 cron/Windows 任务计划, 每日凌晨执行

输出: data/*.parquet (snappy 压缩, 列式存储, 读取速度 5-10x vs SQLite)

SQLite 源表 → Parquet 目标表:
  ① training_extended   (311K rows × 45 cols) → training_extended.parquet
  ② historical_matches   (312K rows × 19 cols) → historical_matches.parquet
  ③ odds_features        (302K rows × 27 cols) → odds_features.parquet
  ④ match_features       (33K rows  × 84 cols) → match_features.parquet
  ⑤ matches              (33K rows  × 21 cols) → matches.parquet
  ⑥ william_ht           (458K rows × 19 cols) → william_ht.parquet  [省电模式跳过]
"""

import os
import sys
import sqlite3
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ── Parquet 引擎自适应 ──
_PARQUET_ENGINE = None
def _detect_engine():
    global _PARQUET_ENGINE
    if _PARQUET_ENGINE is not None:
        return _PARQUET_ENGINE
    for eng in ['fastparquet', 'pyarrow']:
        try:
            __import__(eng)
            _PARQUET_ENGINE = eng
            return eng
        except ImportError:
            continue
    raise ImportError("需要 fastparquet 或 pyarrow: pip install fastparquet")

_ENGINE = _detect_engine()

# ── 路径配置 ──
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "football_data.db"

# ── 同步清单 ──
# (表名, 文件名, 行数下限, 省电模式是否包含)
SYNC_TABLES = [
    ("training_extended",  "training_extended.parquet",   300_000, True),
    ("historical_matches", "historical_matches.parquet",  300_000, True),
    ("odds_features",      "odds_features.parquet",       300_000, True),
    ("match_features",     "match_features.parquet",       30_000, True),
    ("matches",            "matches.parquet",              30_000, True),
    ("william_ht",         "william_ht.parquet",          450_000, False),  # 省电模式跳过
]

logger = logging.getLogger("ParquetSync")

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

def get_table_rowcount(db_path: str, table: str) -> int:
    """快速获取表行数 (避免全表扫描)"""
    with sqlite3.connect(db_path) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]

def sqlite_to_parquet(
    db_path: str,
    table: str,
    output_path: str,
    batch_size: int = 50_000,
    eco_mode: bool = False,
) -> dict:
    """
    单表同步: SQLite → Parquet

    Args:
        db_path: SQLite 数据库路径
        table: 源表名
        output_path: 输出 .parquet 文件路径
        batch_size: 分批读取行数 (省电模式 10_000)
        eco_mode: 省电模式 — 更小批次, 更低内存

    Returns:
        {"table": str, "rows": int, "cols": int, "elapsed_s": float, "size_mb": float}
    """
    batch_size = min(batch_size, 10_000) if eco_mode else batch_size
    t0 = time.time()

    with sqlite3.connect(db_path) as conn:
        # 首次读取获取 schema + 第一批数据
        first_batch = pd.read_sql_query(
            f"SELECT * FROM [{table}] LIMIT 0", conn
        )
        columns = first_batch.columns.tolist()
        total_rows = get_table_rowcount(db_path, table)

        if total_rows == 0:
            logger.warning(f"  {table}: 空表, 跳过")
            return {"table": table, "rows": 0, "cols": len(columns),
                    "elapsed_s": 0, "size_mb": 0}

        # 分批读取 + 写入 (追加模式)
        logger.info(f"  {table}: {total_rows:,} rows × {len(columns)} cols, batch={batch_size:,}")

        offset = 0
        first_file = True

        import tqdm
        pbar = tqdm.tqdm(total=total_rows, desc=f"  {table}", unit="rows",
                         bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")

        while offset < total_rows:
            chunk = pd.read_sql_query(
                f"SELECT * FROM [{table}] LIMIT {batch_size} OFFSET {offset}",
                conn,
            )

            if first_file:
                chunk.to_parquet(output_path, index=False, compression="snappy", engine=_ENGINE)
                first_file = False
            else:
                # 追加到已有 parquet (需要先读取再 concat)
                existing = pd.read_parquet(output_path, engine=_ENGINE)
                combined = pd.concat([existing, chunk], ignore_index=True)
                combined.to_parquet(output_path, index=False, compression="snappy", engine=_ENGINE)

            offset += len(chunk)
            pbar.update(len(chunk))

            if eco_mode and offset % (batch_size * 2) == 0:
                time.sleep(0.05)  # 省电: 间歇放行 IO

        pbar.close()

    elapsed = time.time() - t0
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(f"  {table}: ✅ {total_rows:,} rows → {output_path} ({size_mb:.1f}MB) in {elapsed:.1f}s")

    return {
        "table": table, "rows": total_rows, "cols": len(columns),
        "elapsed_s": elapsed, "size_mb": size_mb,
    }

def run_sync(
    db_path: str = None,
    output_dir: str = None,
    eco_mode: bool = False,
    tables: list = None,
) -> dict:
    """
    执行全量同步

    Returns:
        {"status": "ok"/"partial", "tables": [...], "total_rows": int, "total_size_mb": float}
    """
    db_path = db_path or str(DB_PATH)
    output_dir = Path(output_dir or DATA_DIR)

    if not os.path.exists(db_path):
        return {"status": "error", "error": f"数据库不存在: {db_path}"}

    sync_list = tables or SYNC_TABLES
    if eco_mode:
        sync_list = [t for t in sync_list if t[3]]  # 省电模式: 仅核心表

    logger.info(f"钱代驾 SQLite→Parquet 同步启动")
    logger.info(f"  数据库: {db_path} ({os.path.getsize(db_path)//(1024*1024):.0f}MB)")
    logger.info(f"  输出: {output_dir}")
    logger.info(f"  模式: {'省电' if eco_mode else '标准'}")
    logger.info(f"  表数: {len(sync_list)}")

    results = []
    for table, filename, min_rows, _ in sync_list:
        try:
            actual_rows = get_table_rowcount(db_path, table)
            if actual_rows < min_rows * 0.5:
                logger.warning(f"  {table}: 行数异常 ({actual_rows:,} < {min_rows:,}), 跳过")
                continue

            output_path = str(output_dir / filename)
            result = sqlite_to_parquet(db_path, table, output_path, eco_mode=eco_mode)
            results.append(result)

        except Exception as e:
            logger.error(f"  {table}: ❌ {e}")
            results.append({"table": table, "error": str(e)})

    total_rows = sum(r.get("rows", 0) for r in results)
    total_mb = sum(r.get("size_mb", 0) for r in results)
    errors = [r for r in results if "error" in r]

    status = "ok" if not errors else "partial"

    logger.info(f"同步完成: {len(results)-len(errors)}/{len(results)} 表 ✅, "
                f"{total_rows:,} rows, {total_mb:.1f}MB")

    if errors:
        logger.warning(f"  {len(errors)} 表失败: {[e['table'] for e in errors]}")

    return {
        "status": status,
        "sync_time": datetime.now(timezone.utc).isoformat(),
        "mode": "eco" if eco_mode else "standard",
        "tables": results,
        "total_rows": total_rows,
        "total_size_mb": round(total_mb, 1),
        "errors": [e["table"] for e in errors],
    }

def verify_parquet(output_dir: str = None) -> dict:
    """验证已同步的 Parquet 文件完整性"""
    output_dir = Path(output_dir or DATA_DIR)
    report = {}
    for _, filename, min_rows, _ in SYNC_TABLES:
        path = output_dir / filename
        if not path.exists():
            report[filename] = {"status": "missing", "rows": 0}
            continue
        df = pd.read_parquet(path, engine=_ENGINE)
        ok = len(df) >= min_rows
        report[filename] = {
            "status": "ok" if ok else "degraded",
            "rows": len(df),
            "cols": len(df.columns),
            "size_mb": round(os.path.getsize(path) / (1024 * 1024), 1),
        }
    return report

# ═══ CLI ═══
if __name__ == "__main__":
    setup_logging()

    parser = argparse.ArgumentParser(description="钱代驾 — SQLite→Parquet 同步")
    parser.add_argument("--eco", action="store_true", help="省电模式 (低内存, 仅核心表)")
    parser.add_argument("--verify", action="store_true", help="仅验证已有 Parquet")
    parser.add_argument("--db", type=str, help="SQLite 路径 (默认 data/football_data.db)")
    parser.add_argument("--out", type=str, help="输出目录 (默认 data/)")
    args = parser.parse_args()

    if args.verify:
        report = verify_parquet(args.out)
        print("\n=== Parquet 完整性验证 ===")
        for fname, info in report.items():
            mark = "✅" if info["status"] == "ok" else "⚠️" if info["status"] == "degraded" else "❌"
            print(f"  {mark} {fname}: {info['rows']:,} rows × {info['cols']} cols, {info['size_mb']}MB")
    else:
        result = run_sync(args.db, args.out, eco_mode=args.eco)
        print(f"\n钱代驾同步完成: {result['status']}, {result['total_rows']:,} rows, {result['total_size_mb']}MB")
        if result.get("errors"):
            print(f"  ⚠️ 失败表: {result['errors']}")
