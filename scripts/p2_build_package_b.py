r"""p2_build_package_b.py — 组装哨响AI Package B 赔率资产交付包（只读导出，不碰 events.db）

目的: 把最高价值资产——赔率时序(odds_changes / odds_snapshots)抽成独立交付 SQLite,
     供董事长走外部法律确认(P2-5)后另售。保底锚 ¥85–110万 的实体支撑。

铁律(DISCIPLINE.md §4): 对 events.db 严格只读(ATTACH mode=ro), 绝不写源库/不 VACUUM/不 rm。
     只写新交付文件 deliverables/p2_package_b/。
     默认不含 _bak 内部备份表(只交付产品, 不交付我们的安全副本); --with-bak 可选。

用法:
  # 样本验证(快速, 证明管线正确, 默认 20万行/表)
  .venv/Scripts/python.exe scripts/p2_build_package_b.py --sample 200000

  # 全量导出(重型: ~1.66亿行 odds_changes+odds_snapshots; ATTACH+CTAS 批量, 只读源库)
  .venv/Scripts/python.exe scripts/p2_build_package_b.py --full

  # 含 bak 备份表
  .venv/Scripts/python.exe scripts/p2_build_package_b.py --full --with-bak
"""
import os
import sqlite3
import argparse
import time
import json
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "deliverables", "p2_package_b")
OUT_DB = os.path.join(OUT_DIR, "p2_package_B.sqlite")
MANIFEST = os.path.join(OUT_DIR, "MANIFEST.json")

SRC_DB = os.path.join(DATA, "events.db")
ODDS_TABLES = ["odds_changes", "odds_snapshots"]
ODDS_TABLES_BAK = ["odds_changes_bak", "odds_snapshots_bak"]


def copy_sample(src_con, dst_con, table, limit):
    cur = src_con.cursor()
    cnt = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"  [{table}] 源 {cnt:,} 行 → 抽样 {limit:,} 行")
    rows = cur.execute(f"SELECT * FROM {table} ORDER BY rowid LIMIT ?", (limit,)).fetchall()
    if not rows:
        print(f"  [{table}] 空表, 跳过"); return 0
    cols = [d[0] for d in cur.description]
    col_defs = ", ".join(f'"{c}"' for c in cols)
    dst_con.execute(f'DROP TABLE IF EXISTS "{table}"')
    try:
        schema = src_con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()[0]
        if schema:
            dst_con.execute(schema)
        else:
            dst_con.execute(f'CREATE TABLE "{table}"({col_defs})')
    except Exception:
        dst_con.execute(f'CREATE TABLE "{table}"({col_defs})')
    ph = ", ".join("?" for _ in cols)
    dst_con.executemany(f'INSERT INTO "{table}" VALUES ({ph})', rows)
    dst_con.commit()
    return len(rows)


def copy_full_bulk(dst_db_path, tables, batch=200000):
    """只读源库(connect mode=ro, 已验证) + 按 id 分块 SELECT 拷贝。稳且严格只读。
    返回 {表: 行数}。"""
    src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_db_path)
    res = {}
    for t in tables:
        try:
            mn, mx = src.execute(f"SELECT MIN(id), MAX(id) FROM {t}").fetchone()
        except Exception as e:
            print(f"  [{t}] 源读取失败: {e}"); res[t] = f"ERR:{e}"; continue
        if mn is None:
            print(f"  [{t}] 空表, 跳过"); res[t] = 0; continue
        ncols = [d[0] for d in src.execute(f"SELECT * FROM {t} WHERE id=?", (mn,)).description]
        dst.execute(f'DROP TABLE IF EXISTS "{t}"')
        schema = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()[0]
        if schema:
            dst.execute(schema)
        else:
            dst.execute(f'CREATE TABLE "{t}"(' + ", ".join(f'"{c}"' for c in ncols) + ")")
        ph = ", ".join("?" for _ in ncols)
        total = 0
        done = mn
        while done <= mx:
            rows = src.execute(
                f"SELECT * FROM {t} WHERE id BETWEEN ? AND ?", (done, done + batch - 1)
            ).fetchall()
            if rows:
                dst.executemany(f'INSERT INTO "{t}" VALUES ({ph})', rows)
                total += len(rows)
            done += batch
        dst.commit()
        res[t] = total
        print(f"  [{t}] 完成 {total:,} 行")
    src.close(); dst.close()
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量导出(重型, ATTACH+CTAS)")
    ap.add_argument("--sample", type=int, default=200000, help="抽样行数/表")
    ap.add_argument("--with-bak", action="store_true", help="含 _bak 备份表")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    tables = list(ODDS_TABLES)
    if args.with_bak:
        tables += ODDS_TABLES_BAK

    print(f"[Package B 导出] 模式={'全量CTAS' if args.full else f'抽样{args.sample:,}/表'} "
          f"含bak={args.with_bak} 源={SRC_DB}")
    t0 = time.time()
    per_table = {}
    if args.full:
        per_table = copy_full_bulk(OUT_DB, tables)
    else:
        src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
        dst = sqlite3.connect(OUT_DB)
        for t in tables:
            per_table[t] = copy_sample(src, dst, t, args.sample)
        src.close(); dst.close()
    total = sum(v for v in per_table.values() if isinstance(v, int))

    manifest = {
        "package": "B",
        "title": "赔率时序资产(odds_changes / odds_snapshots)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "full_ctas" if args.full else f"sample_{args.sample}",
        "with_bak": args.with_bak,
        "source_db": "events.db (READ-ONLY ATTACH, 未修改)",
        "tables": per_table,
        "total_rows_exported": total,
        "legal_gate": "P2-5 外部法律确认前不得售卖(含用户/密钥硬排除)",
        "note": "保底锚 ¥85-110万 实体支撑; 导出仅读源库, 不碰 events.db",
    }
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n[完成] 写出 {total:,} 行 → {OUT_DB}")
    print(f"MANIFEST → {MANIFEST}  耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
