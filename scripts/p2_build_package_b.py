r"""p2_build_package_b.py — 组装哨响AI Package B 赔率资产交付包（只读导出，不碰 events.db）

目的: 把最高价值资产——赔率时序(odds_changes / odds_snapshots, 含 _bak 备份)抽成独立交付
      SQLite, 供老板走外部法律确认(P2-5)后另售。这是系统保底锚 ¥85–110万 的实体支撑。

铁律(源自 DISCIPLINE.md):
- 对 events.db 严格只读(READ-ONLY), 绝不写源库、绝不 VACUUM、绝不 rm。
- 只写新交付文件 deliverables/p2_package_b/。
- 导出表: odds_changes, odds_snapshots (主); odds_changes_bak, odds_snapshots_bak (可选 --with-bak)。
- 不导出 users / 任何含密钥表。

用法:
  # 样本验证(快速, 证明管线正确, 默认 20万行/表)
  D:\Architecture\.venv\Scripts\python.exe scripts\p2_build_package_b.py --sample 200000

  # 全量导出(重型: ~2.2亿行, 写多 GB sqlite; 须老板确认 + 法律门禁 P2-5)
  D:\Architecture\.venv\Scripts\python.exe scripts\p2_build_package_b.py --full

  # 含 bak 备份表
  D:\Architecture\.venv\Scripts\python.exe scripts\p2_build_package_b.py --full --with-bak
"""
import os
import sqlite3
import argparse
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "deliverables", "p2_package_b")
OUT_DB = os.path.join(OUT_DIR, "p2_package_B.sqlite")
MANIFEST = os.path.join(OUT_DIR, "MANIFEST.json")

SRC_DB = os.path.join(DATA, "events.db")

ODDS_TABLES = ["odds_changes", "odds_snapshots"]
ODDS_TABLES_BAK = ["odds_changes_bak", "odds_snapshots_bak"]


def ro_connect(path):
    """严格只读连接: mode=ro 防止任何意外写。"""
    uri = f"file:{path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def copy_table(src_con, dst_con, table, limit=None):
    cur = src_con.cursor()
    cnt = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if limit and limit < cnt:
        print(f"  [{table}] 源 {cnt:,} 行 → 抽样 {limit:,} 行")
        sql = f"SELECT * FROM {table} ORDER BY rowid LIMIT ?"
        rows = cur.execute(sql, (limit,)).fetchall()
    else:
        print(f"  [{table}] 全量 {cnt:,} 行")
        rows = cur.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"  [{table}] 空表, 跳过")
        return 0
    # 建表结构(从首行 + 列名)
    cols = [d[0] for d in cur.description]
    col_defs = ", ".join(f'"{c}"' for c in cols)
    dst_con.execute(f'DROP TABLE IF EXISTS "{table}"')
    # 用原表 schema 建
    try:
        schema = src_con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        dst_con.execute(schema.replace(f'CREATE TABLE "{table}"', f'CREATE TABLE "{table}"', 1)
                        if schema else f'CREATE TABLE "{table}"({col_defs})')
    except Exception:
        dst_con.execute(f'CREATE TABLE "{table}"({col_defs})')
    placeholders = ", ".join("?" for _ in cols)
    dst_con.executemany(f'INSERT INTO "{table}" VALUES ({placeholders})', rows)
    dst_con.commit()
    n = len(rows)
    print(f"  [{table}] 写出 {n:,} 行")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量导出(重型)")
    ap.add_argument("--sample", type=int, default=200000, help="抽样行数/表(默认20万)")
    ap.add_argument("--with-bak", action="store_true", help="含 _bak 备份表")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    tables = list(ODDS_TABLES)
    if args.with_bak:
        tables += ODDS_TABLES_BAK
    limit = None if args.full else args.sample

    print(f"[Package B 导出] 模式={'全量' if args.full else f'抽样{args.sample:,}/表'} "
          f"含bak={args.with_bak} 源={SRC_DB}")
    t0 = time.time()
    src = ro_connect(SRC_DB)
    dst = sqlite3.connect(OUT_DB)
    dst.execute("PRAGMA journal_mode=DELETE")
    total = 0
    per_table = {}
    for t in tables:
        try:
            n = copy_table(src, dst, t, limit)
            per_table[t] = n
            total += n
        except Exception as e:
            print(f"  [{t}] 导出失败: {e}")
            per_table[t] = f"ERR:{e}"
    src.close()
    dst.close()

    manifest = {
        "package": "B",
        "title": "赔率时序资产(odds_changes / odds_snapshots)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "full" if args.full else f"sample_{args.sample}",
        "with_bak": args.with_bak,
        "source_db": "events.db (READ-ONLY, 未修改)",
        "tables": per_table,
        "total_rows_exported": total,
        "legal_gate": "P2-5 外部法律确认前不得售卖(含用户/密钥硬排除)",
        "note": "保底锚 ¥85-110万 实体支撑; 导出仅读源库, 不碰 events.db",
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        import json
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    dt = time.time() - t0
    print(f"\n[完成] 写出 {total:,} 行 → {OUT_DB}")
    print(f"MANIFEST → {MANIFEST}")
    print(f"耗时 {dt:.1f}s")


if __name__ == "__main__":
    main()
