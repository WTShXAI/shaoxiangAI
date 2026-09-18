r"""p2_build_delivery_package.py — 组装哨响AI P2 数据资产交付包（Package A 赛果 + Package C 参考）

目的: 把"可立即打包售卖"的公开体育数据(无PII、无内嵌赔率)抽成独立交付 SQLite,
      应用 clean_outcomes 假0-0 过滤, 硬排除 users/赔率表, 并捆绑字典/质量/合规文档。

铁律:
- 只读源库; 只写新交付文件(不碰源 DB)。
- 硬排除 football_data.users(password_hash) 及所有赔率表(Package B, 合规卡住)。
- match_outcomes(内嵌赔率) 归入 Package B, 不进本包。
- 赛果表过 clean_outcomes: 排除 score_missing=1 的假0-0。

输出: deliverables/p2_delivery_package/
        p2_package_A_C.sqlite   (Package A + C 全部表 + __manifest)
        MANIFEST.json
        docs/  (捆绑 p2_datadict / p2_quality_report / p2_compliance / p2_sellability / p2_asset_catalog)
用法(铁律前缀):
  D:\Architecture\.venv\Scripts\python.exe scripts\p2_build_delivery_package.py
"""
import os, json, shutil, sqlite3, re
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT_DIR = os.path.join(ROOT, "deliverables", "p2_delivery_package")
OUT_DB = os.path.join(OUT_DIR, "p2_package_A_C.sqlite")
DOCS_DIR = os.path.join(OUT_DIR, "docs")

# 源库映射 (库名 -> 文件名)
SRC = {
    "events": os.path.join(DATA, "events.db"),
    "gq": os.path.join(DATA, "GQ.db"),
    "football_data": os.path.join(DATA, "football_data.db"),
}

# Package A · 纯赛果主数据 (无内嵌赔率)  -- (库, 表, 质量备注)
PKG_A = [
    ("events", "matches", "clean_outcomes 过滤 score_missing=1"),
    ("gq", "matches", "clean_outcomes 过滤 score_missing=1"),
    ("football_data", "historical_matches", "31.2万核心赛果; 0-0率7.8%真实, 无打标体系, 原样交付"),
    ("football_data", "matches", "clean_outcomes 过滤 score_missing=1"),
    ("football_data", "fd_matches", "Football-Data 赛事参考(赛果)"),
    ("football_data", "wc_all_matches", "WC2026 赛事"),
    ("football_data", "wc_xlsx_matches", "WC2026 xlsx 赛事"),
    ("football_data", "wc_lineups", "WC2026 首发(公开)"),
    ("football_data", "leisu_results", "雷速赛果(10行,小)"),
]

# Package C · 参考/派生层 (无PII, 可免费/增值)
PKG_C = [
    ("football_data", "betfair_market", "Betfair 市场ID参考"),
    ("football_data", "betting_markets", "市场定义参考"),
    ("football_data", "fd_competitions", "赛事参考"),
    ("football_data", "form_trends", "球队状态趋势(公开派生)"),
    ("football_data", "handicap_depth_profile", "同盘口统计剖面(足球AI独家资产)"),
    ("football_data", "handicap_labels", "让球盘口标签(参考资产)"),
    ("football_data", "stadiums", "球场参考"),
    ("football_data", "standings", "积分榜(公开派生)"),
    ("football_data", "team_canonical", "队名规范映射(参考资产)"),
    ("football_data", "teams", "球队主表(参考资产)"),
    ("football_data", "upset_matches", "冷门场次(公开派生)"),
    ("football_data", "weather_data", "天气数据"),
]

# 硬排除(绝不进包)
FORBIDDEN = {"users", "odds", "odds_snapshots", "odds_changes", "odds_features",
             "odds_history", "william_ht", "live_odds_raw", "ou_live_feed",
             "match_outcomes", "interwetten_odds", "leisu_odds", "leisu_odds_legacy",
             "ou_validation", "ou_validation_local", "odds_timeline"}
# 注: match_outcomes 因内嵌赔率归入 Package B(合规卡住), 本包不含。

def ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60)

def cols_of(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()]

def copy_table(src_con, dest_con, db_name, table, note):
    cols = cols_of(src_con, table)
    if not cols:
        return {"db": db_name, "table": table, "target": None, "status": "SKIP_NO_SCHEMA",
                "source_rows": 0, "shipped_rows": 0, "clean": None, "note": note}
    # 安全校验: 硬排除表(users/赔率等) 绝不进包
    if table.lower() in FORBIDDEN:
        return {"db": db_name, "table": table, "target": None, "status": "SKIP_FORBIDDEN",
                "source_rows": 0, "shipped_rows": 0, "clean": None, "note": "硬排除(合规/密钥)"}
    # 命名空间化目标表名, 避免 events.matches / gq.matches / football_data.matches 互相覆盖
    target = f"{db_name}__{table}"
    create_sql = src_con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    new_sql = re.sub(r'CREATE TABLE\s+(["\']?)' + re.escape(table) + r'["\']?',
                     f'CREATE TABLE "{target}"', create_sql, count=1, flags=re.IGNORECASE)
    dest_con.execute(f'DROP TABLE IF EXISTS "{target}"')
    dest_con.execute(new_sql)
    # clean_outcomes 过滤
    has_flag = "score_missing" in [c.lower() for c in cols]
    where = " WHERE COALESCE(score_missing,0) != 1" if has_flag else ""
    total_rows = src_con.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0]
    cur = src_con.execute(f"SELECT * FROM '{table}'{where}")
    rows = cur.fetchall()
    ph = ",".join("?" * len(cols))
    col_list = ",".join(f'"{c}"' for c in cols)
    dest_con.executemany(f'INSERT INTO "{target}" ({col_list}) VALUES ({ph})', rows)
    dest_con.commit()
    return {"db": db_name, "table": table, "target": target, "status": "OK",
            "source_rows": total_rows, "shipped_rows": len(rows),
            "clean": ("score_missing!=1" if has_flag else "n/a"), "note": note}

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(DOCS_DIR, exist_ok=True)
    dest = sqlite3.connect(OUT_DB)
    dest.execute("PRAGMA foreign_keys=OFF")

    manifest = {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
                "package": "P2 数据资产交付包 (Package A 赛果 + Package C 参考)",
                "compliance": "无PII; 排除 users(password_hash); 排除赔率表(Package B 合规卡住); match_outcomes 内嵌赔率不进包",
                "tables": []}

    print("=" * 64)
    print("P2 数据资产交付包组装")
    print("=" * 64)
    for pkg, items in (("A", PKG_A), ("C", PKG_C)):
        for db_name, table, note in items:
            sp = SRC[db_name]
            if not os.path.exists(sp):
                print(f"  ⚠️ [{pkg}] {db_name}.{table} 源库缺失 {sp} -> SKIP")
                manifest["tables"].append({"pkg": pkg, "db": db_name, "table": table,
                                            "status": "SKIP_NO_DB", "note": "源库缺失"})
                continue
            with ro(sp) as sc:
                r = copy_table(sc, dest, db_name, table, note)
            r["pkg"] = pkg
            manifest["tables"].append(r)
            tag = "✅" if r["status"] == "OK" else "⚠️"
            print(f"  {tag} [{pkg}] {db_name}.{table}: {r['shipped_rows']:,}/{r['source_rows']:,} 行"
                  + (f"  (clean={r['clean']})" if r.get("clean") else ""))

    # manifest 落库 + JSON
    dest.execute("DROP TABLE IF EXISTS __manifest")
    dest.execute("""CREATE TABLE __manifest (
        pkg TEXT, db TEXT, tbl TEXT, status TEXT,
        source_rows INTEGER, shipped_rows INTEGER, clean TEXT, note TEXT)""")
    for r in manifest["tables"]:
        dest.execute("INSERT INTO __manifest VALUES (?,?,?,?,?,?,?,?)",
                     (r.get("pkg"), r["db"], r["table"], r["status"],
                      r.get("source_rows", 0), r.get("shipped_rows", 0),
                      r.get("clean"), r.get("note")))
    dest.commit()
    dest.close()

    # 捆绑文档
    doc_src = {
        "数据字典.md": os.path.join(ROOT, "reports", "p2_datadict.md"),
        "质量报告.md": os.path.join(ROOT, "reports", "p2_quality_report.md"),
        "合规边界.md": os.path.join(ROOT, "reports", "p2_compliance.md"),
        "可售性评估.md": os.path.join(ROOT, "reports", "p2_sellability.md"),
        "资产目录.md": os.path.join(ROOT, "reports", "p2_asset_catalog.md"),
    }
    bundled = []
    for name, src in doc_src.items():
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DOCS_DIR, name))
            bundled.append(name)

    with open(os.path.join(OUT_DIR, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    ok = [r for r in manifest["tables"] if r["status"] == "OK"]
    print("-" * 64)
    print(f"交付库: {OUT_DB}")
    print(f"表数: {len(ok)} OK / {len(manifest['tables'])} 总")
    print(f"捆绑文档: {', '.join(bundled)}")
    print(f"MANIFEST: {os.path.join(OUT_DIR,'MANIFEST.json')}")

if __name__ == "__main__":
    main()
