"""P2-2 数据字典 — 对资产目录中「可售+须脱敏」表做字段级 introspect

复用 reports/p2_asset_catalog.json(由 p2_asset_inventory.py 生成)。
对每张表: PRAGMA table_info(列名/类型/非空/默认) + 采样 1 行推断口径/单位。
内部表(INTERNAL)不进字典(非数据产品)。

输出:
  reports/p2_datadict.json
  reports/p2_datadict.md   按 库/表 分组,逐字段: 名/类型/口径/单位/示例/非空

用法: .venv/Scripts/python.exe scripts/p2_datadict.py
"""
import json, os, sqlite3
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "reports")
CAT = os.path.join(OUT, "p2_asset_catalog.json")
DB_DIR = os.path.join(ROOT, "data")

# 口径/单位推断(按列名关键字)
def infer_unit(col, ctype):
    c = col.lower()
    if any(k in c for k in ("odds", "price", "quote", "line", "handicap", "ah_", "ou_")):
        return "赔率(欧式小数, 域(0,1000])"
    if any(k in c for k in ("prob", "_p", "p_", "win_rate", "implied", "edge")):
        return "概率/比例[0,1]"
    if any(k in c for k in ("score", "goals", "ht_", "ft_", "total_goals")):
        return "整数(比分/进球)"
    if c in ("home", "away"):
        return "队名(文本)"
    if any(k in c for k in ("margin", "vig", "commission", "rake")):
        return "庄家抽水[0,1]或%"
    if any(k in c for k in ("captured_at", "kickoff", "created_at", "last_seen",
                            "timestamp", "ts", "time", "_at", "updated")):
        return "时间戳(unix秒 或 ISO)"
    if "key" in c or "id" in c:
        return "标识符"
    if ctype.upper() in ("INTEGER", "REAL", "FLOAT", "NUMERIC"):
        return "数值"
    if ctype.upper() in ("TEXT",):
        return "文本"
    return "未知"


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def main():
    cat = json.load(open(CAT, encoding="utf-8"))
    assets = [a for a in cat["assets"] if a["category"] != "INTERNAL"]
    # 同表可能多库重复,按 (db,table) 去重
    seen = set()
    entries = []
    for a in assets:
        key = (a["db"], a["table"])
        if key in seen:
            continue
        seen.add(key)
        rel = {
            "events": "data/events.db", "gq": "data/GQ.db",
            "football_data": "data/football_data.db",
            "rollball_train": "data/rollball_training.db",
            "leisu_odds": "data/leisu_odds.db",
        }[a["db"]]
        path = os.path.join(ROOT, rel)
        try:
            c = ro(path); cur = c.cursor()
            cols = cur.execute(f'PRAGMA table_info("{a["table"]}")').fetchall()
            sample = None
            try:
                sample = cur.execute(f'SELECT * FROM "{a["table"]}" LIMIT 1').fetchone()
            except Exception:
                pass
            fields = []
            for i, (cid, name, ctype, notnull, dflt, pk) in enumerate(cols):
                unit = infer_unit(name, ctype)
                ex = sample[i] if sample is not None and i < len(sample) else None
                if isinstance(ex, (bytes,)):
                    ex = f"<{len(ex)}B blob>"
                elif ex is not None and len(str(ex)) > 40:
                    ex = str(ex)[:40] + "…"
                fields.append({
                    "name": name, "type": ctype, "notnull": notnull,
                    "pk": pk, "unit": unit, "example": ex,
                })
            entries.append({
                "db": a["db"], "table": a["table"],
                "category": a["category"], "value": a["value"],
                "row_count": a["rows"], "fields": fields,
            })
            c.close()
        except Exception as e:
            entries.append({"db": a["db"], "table": a["table"],
                            "category": a["category"], "error": str(e)[:120]})

    out = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "tables_documented": len(entries),
        "entries": entries,
    }
    jp = os.path.join(OUT, "p2_datadict.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    # MD
    md = ["# 哨响AI 数据字典（P2-2）\n",
          f"> 生成: {out['generated_at']} | 文档化表数: {len(entries)}（仅可售+须脱敏,内部表不收录）\n",
          "> 单位推断为启发式(按列名),赔率统一记欧式小数域(0,1000]。完整口径以 P2-3 质量报告为准。\n"]
    for e in entries:
        md.append(f"\n## {e['db']}.{e['table']}  "
                  f"[{('可售' if e['category']=='SELLABLE' else '须脱敏')} / {e.get('value','')}]\n")
        rc = e.get("row_count")
        md.append(f"> 行数: {rc:,}\n" if rc is not None else "> 行数: LARGE\n")
        if "error" in e:
            md.append(f"> ⚠ 读取失败: {e['error']}\n")
            continue
        md.append("| # | 字段 | 类型 | 非空 | 单位/口径 | 示例 |")
        md.append("|---|---|---|---|---|---|")
        for f in e["fields"]:
            ex = "" if f["example"] is None else str(f["example"])
            md.append(f"| {f['pk'] and 'PK' or ''} | {f['name']} | {f['type']} | "
                      f"{'Y' if f['notnull'] else ''} | {f['unit']} | {ex} |")
    mp = os.path.join(OUT, "p2_datadict.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"[p2_datadict] 文档化表={len(entries)}  -> {jp}\n  -> {mp}")


if __name__ == "__main__":
    main()
