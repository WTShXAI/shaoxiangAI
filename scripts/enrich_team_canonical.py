# -*- coding: utf-8 -*-
"""
队名治理: 把 long/ 截图 OCR 的未命中队名补入 team_canonical。
策略:
  - 从 prematch_subset.csv 提取所有去重队名
  - 剔除噪声 (单字/符号/OCR碎片)
  - 已存在于 team_canonical 的跳过 (INSERT OR IGNORE 天然防重)
  - 新队名 canonical=OCR名, aliases_json=[OCR名], note标来源+级别
  - U23/青年/女足 在 note 标注级别, 便于后续按级别过滤
输出:
  - 直接写入 football_data.db.team_canonical
  - data/long_features/team_canonical_enrich_report.json (增补清单)
"""
import sqlite3, json, csv, re

DB = r"D:\Architecture\data\football_data.db"
SUBSET = r"D:\Architecture\data\long_features\prematch_subset.csv"
REPORT = r"D:\Architecture\data\long_features\team_canonical_enrich_report.json"

# 噪声黑名单 (OCR碎片/界面文字, 非队名)
BLACKLIST = {"O","个","三","口","巴","起·","☆白","☆中","☆上","☆VS","王胜","超新星",
             "主0","客0","特色组合√","波胆入","即将开赛","场让球","永久禁言中"}

def classify_level(name):
    """判断队名级别。返回 (level, is_noise)。"""
    ns = name.strip()
    if not ns or len(ns) < 2 or ns in BLACKLIST:
        return None, True
    if re.search(r"[√↑]", ns) or ns in BLACKLIST:
        return None, True
    if "U23" in ns or "U21" in ns or "U2" in ns or "青年" in ns or "后备" in ns:
        return "youth", False
    if "(女)" in ns or "（女）" in ns:
        return "women", False
    return "senior", False

def main():
    rows = list(csv.DictReader(open(SUBSET, encoding="utf-8-sig")))
    names = set()
    for r in rows:
        for side in ("home", "away"):
            n = (r.get(side) or "").strip()
            if n: names.add(n)

    con = sqlite3.connect(DB); cur = con.cursor()
    # 现有 canonical 集合
    existing = {r[0] for r in cur.execute("SELECT canonical FROM team_canonical")}

    inserted = []
    skipped_exist = 0
    skipped_noise = 0
    for n in sorted(names):
        level, is_noise = classify_level(n)
        if is_noise:
            skipped_noise += 1; continue
        if n in existing:
            skipped_exist += 1; continue
        # INSERT OR IGNORE (主键冲突保护)
        aliases = json.dumps([n], ensure_ascii=False)
        note = f"long截图OCR补入 | level={level}"
        try:
            cur.execute("INSERT OR IGNORE INTO team_canonical(canonical,aliases_json,note) VALUES(?,?,?)",
                        (n, aliases, note))
            if cur.rowcount > 0:
                inserted.append({"canonical": n, "level": level})
        except sqlite3.IntegrityError:
            skipped_exist += 1
    con.commit()

    # 统计各级别
    from collections import Counter
    lvl = Counter(x["level"] for x in inserted)
    report = {
        "输入去重队名": len(names),
        "新补入": len(inserted),
        "  - 成年队": lvl.get("senior", 0),
        "  - U23/青年": lvl.get("youth", 0),
        "  - 女足": lvl.get("women", 0),
        "已存在跳过": skipped_exist,
        "噪声剔除": skipped_noise,
        "补入清单": inserted,
    }
    json.dump(report, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    con.close()

    print(f"=== team_canonical 补入完成 ===")
    print(f"输入去重队名: {len(names)}")
    print(f"新补入: {len(inserted)} (成年{lvl.get('senior',0)} / 青年{lvl.get('youth',0)} / 女足{lvl.get('women',0)})")
    print(f"已存在跳过: {skipped_exist} | 噪声剔除: {skipped_noise}")
    print(f"→ {REPORT}")
    # 验证: 总数
    con2 = sqlite3.connect(DB)
    total = con2.execute("SELECT COUNT(*) FROM team_canonical").fetchone()[0]
    print(f"team_canonical 现总数: {total}")
    con2.close()

if __name__ == "__main__":
    main()
