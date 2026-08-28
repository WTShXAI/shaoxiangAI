# -*- coding: utf-8 -*-
"""
队名规范化映射 — 把特征表的 App 显示队名映射到 team_canonical.canonical。
复用 pipeline.reverse_odds_engine 的 _build_alias_map / _resolve_canonical (跨语言归一)。
输出:
  data/long_features/match_features_canon.csv  — 增加 home_canonical/away_canonical 列
  data/long_features/team_mapping_report.json  — 命中率 + 未命中清单
"""
import os, sys, json, csv, sqlite3, re
from typing import List, Optional, Tuple

DB = r"D:\Architecture\data\football_data.db"

# ── 复制自 pipeline/reverse_odds_engine.py (避免引入 pandas 依赖) ──
def _latin_key(s: str) -> str:
    if not s: return ''
    toks = re.findall(r'[A-Za-z]+', str(s))
    return ''.join(toks).lower()

def _build_alias_map(db_path: str) -> List[Tuple[str, str]]:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT canonical, aliases_json FROM team_canonical").fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    amap = []
    for canon, aj in rows:
        try: al = json.loads(aj) if aj else []
        except Exception: al = []
        if isinstance(al, list):
            for a in al: amap.append((_latin_key(str(a)), canon))
        amap.append((_latin_key(str(canon)), canon))
    return amap

def _resolve_canonical(name: str, alias_map: List[Tuple[str, str]]) -> Optional[str]:
    if not name: return None
    nl = _latin_key(name)
    # ⚠️ latin_key < 4 字符 (如 "fc"/"ac"/"ce") 极易碰撞, 不走 latin 匹配
    if nl and len(nl) >= 4:
        for lk, canon in alias_map:
            if lk and lk == nl: return canon
    s = str(name).strip()
    # 中文精确匹配优先
    for lk, canon in alias_map:
        if s == canon: return canon
    # 中文包含匹配: 队名是 canonical 的前缀或反之 (处理 "大邱FC" 等)
    if re.search(r"[\u4e00-\u9fa5]", s):
        # 先找完全以中文部分命中的
        zh_part = re.sub(r"[A-Za-z()（）]", "", s).strip()
        if zh_part:
            for lk, canon in alias_map:
                if zh_part == canon or (zh_part in canon and len(zh_part) >= 2):
                    return canon
    return None

IN_CSV = r"D:\Architecture\data\long_features\match_features.csv"
OUT_CSV = r"D:\Architecture\data\long_features\match_features_canon.csv"
REPORT = r"D:\Architecture\data\long_features\team_mapping_report.json"

def main():
    alias_map = _build_alias_map(DB)
    print(f"team_canonical 别名表: {len(alias_map)} 条")
    rows = list(csv.DictReader(open(IN_CSV, encoding="utf-8-sig")))
    fields = list(rows[0].keys()) if rows else []
    # 插入 canonical 列 (在 home/away 之后)
    for col in ("home_canonical", "away_canonical"):
        if col not in fields:
            fields.insert(fields.index("away") + 1 if "away" in fields else len(fields), col)

    hit = miss = 0
    miss_names = set()
    for r in rows:
        for side in ("home", "away"):
            raw = (r.get(side) or "").strip()
            if not raw or any(c in raw for c in ("√", "↑", "场让", "即将", "波胆")):
                r[side + "_canonical"] = ""
                continue
            canon = _resolve_canonical(raw, alias_map)
            r[side + "_canonical"] = canon or ""
            if canon:
                hit += 1
            else:
                miss += 1
                miss_names.add(raw)

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows: w.writerow(r)

    total = hit + miss
    report = {
        "总队名实例": total,
        "命中": hit,
        "未命中": miss,
        "命中率": f"{100*hit/max(total,1):.1f}%",
        "未命中队名清单": sorted(miss_names),
        "未命中原因分析": "特征表多为U23/青年/女足/小联赛队(如奥林匹克秦斯维U23、PK35赫尔辛基), team_canonical(497队, 五大联赛为主)未收录。中甲/中超/日韩部分队名可能需手动补 aliases_json。",
    }
    json.dump(report, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"命中 {hit}/{total} ({report['命中率']})")
    print(f"未命中 {miss} 个队名 (见 report)")
    print(f"→ {OUT_CSV}")
    print(f"→ {REPORT}")
    # 抽样命中
    print("\n--- 命中样本 ---")
    n = 0
    for r in rows:
        hc = r.get("home_canonical")
        if hc and n < 6:
            print(f"  {r.get('home',''):14} → {hc}")
            n += 1

if __name__ == "__main__":
    main()
