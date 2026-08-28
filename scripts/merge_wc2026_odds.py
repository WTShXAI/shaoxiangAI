#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合并 2026 世界杯赔率数据源（规范化版）：
  软件记录(football_data.db.wc_all_matches 2026) -> 1X2 临盘赔率 + 真实赛果
  截图记录(worldcup_screenshots.db)             -> 详细盘口(AH多档/OU多档/CS矩阵)

【名称规范化 (2026-08-12 补】】
  软件库与截图表存在同队异名 -> 输出层直接落规范化名, 保证训练特征一致。
  CANON 映射(原始 -> 规范)：
    乌兹别克          -> 乌兹别克斯坦
    沙特 / 沙特阿拉伯  -> 沙特阿拉伯
    卡特尔 / 卡塔爾    -> 卡塔尔
    明主刚果 / 民主剛果 -> 民主刚果
    佛得角共和国 / 維德角共和國 -> 佛得角
  规范化后去重队名应 = 48 (2026 世界杯 48 队赛制), 反向验证数据真实。

输出：
  wc2026_merged.csv  合并后逐场(赔率来源标记 + 赛果 + 规范队名)
  wc2026_merged.json 结构化(供训练/回填)
"""
import sqlite3, json, re, csv, os

DATA = os.path.dirname(os.path.abspath(__file__)) + "/../data"
FDB = os.path.join(DATA, "football_data.db")
SDB = os.path.join(DATA, "worldcup_screenshots.db")
OUT_CSV = os.path.join(DATA, "wc2026_merged.csv")
OUT_JSON = os.path.join(DATA, "wc2026_merged.json")

# ---- 队名规范化（原始名 -> 规范名） ----
CANON = {
    "乌兹别克": "乌兹别克斯坦",
    "卡特尔": "卡塔尔", "卡塔爾": "卡塔尔",
    "明主刚果": "民主刚果", "民主剛果": "民主刚果",
    "沙特": "沙特阿拉伯", "沙特阿拉伯": "沙特阿拉伯",
    "沙烏地阿拉伯": "沙特阿拉伯",
    "佛得角共和国": "佛得角", "佛得角": "佛得角",
    "維德角共和國": "佛得角",
    "韩国": "韩国", "韓國": "韩国",
}
def canon(t):
    """归一化到规范队名; 命中映射则替换, 否则原样(去空白)。"""
    if t is None:
        return t
    t = t.strip()
    return CANON.get(t, t)

def load_software():
    con = sqlite3.connect(FDB); cur = con.cursor()
    cur.execute("SELECT id,stage,home,away,hg,ag,final_result,oh,od,oa FROM wc_all_matches WHERE edition='2026'")
    rows = []
    for mid, stage, home, away, hg, ag, fr, oh, od, oa in cur.fetchall():
        rows.append(dict(mid=mid, stage=stage,
                         home=canon(home), away=canon(away),
                         raw_home=home, raw_away=away,
                         hg=hg, ag=ag, fr=fr, oh=oh, od=od, oa=oa))
    con.close()
    return rows

def load_shots():
    con = sqlite3.connect(SDB); cur = con.cursor()
    cur.execute("""SELECT id,folder,file,home,away,ft_1x2_h,ft_1x2_d,ft_1x2_a,
                  ht_1x2_h,ht_1x2_d,ht_1x2_a,ft_ah,ft_ou,ht_ah,ht_ou
                  FROM wc_screenshot_matches ORDER BY id""")
    rows = []
    for r in cur.fetchall():
        d = dict(zip(["id","folder","file","home","away","ft_h","ft_d","ft_a",
                      "ht_h","ht_d","ht_a","ft_ah","ft_ou","ht_ah","ht_ou"], r))
        d["home"] = canon(d["home"]); d["away"] = canon(d["away"])
        rows.append(d)
    con.close()
    return rows

soft = load_software()
shots = load_shots()

# 截图按 (home,away) 索引
shot_idx = {}
for s in shots:
    if not s["home"] or not s["away"]:
        s["_valid"] = False
        continue
    s["_valid"] = True
    shot_idx.setdefault((s["home"], s["away"]), []).append(s)

merged = []
stat = dict(software_total=len(soft), shot_total=len(shots),
            shot_valid=sum(1 for s in shots if s.get("_valid")),
            both=0, software_only=0, shot_only=0, shot_broken=0)
name_fix = {}  # raw -> canon 实际发生的修正

def track_fix(raw, canon_name):
    if raw and canon_name and raw != canon_name:
        name_fix.setdefault(raw, canon_name)

# 1) 软件为主，挂截图详细盘口
for m in soft:
    track_fix(m["raw_home"], m["home"]); track_fix(m["raw_away"], m["away"])
    key = (m["home"], m["away"])
    attached = shot_idx.get(key, [])
    shot = attached[0] if attached else None
    rec = dict(
        source_odds="software",
        has_result=1 if (m["hg"] is not None and m["ag"] is not None) else 0,
        home=m["home"], away=m["away"], stage=m["stage"],
        o_h=m["oh"], o_d=m["od"], o_a=m["oa"],
        hg=m["hg"], ag=m["ag"], fr=m["fr"],
        shot_id=shot["id"] if shot else None,
        shot_folder=shot["folder"] if shot else None,
        shot_ft_1x2=(shot["ft_h"], shot["ft_d"], shot["ft_a"]) if shot else None,
        shot_ft_ah=shot["ft_ah"] if shot else None,
        shot_ft_ou=shot["ft_ou"] if shot else None,
        shot_ht_1x2=(shot["ht_h"], shot["ht_d"], shot["ht_a"]) if shot else None,
    )
    merged.append(rec)
    if shot: stat["both"] += 1
    else: stat["software_only"] += 1

# 2) 截图独有（软件无记录）-> 赛果缺失
matched_keys = set((m["home"], m["away"]) for m in soft)
for s in shots:
    if not s.get("_valid"):
        stat["shot_broken"] += 1
        continue
    if (s["home"], s["away"]) in matched_keys:
        continue
    track_fix(s.get("raw_home"), s["home"]); track_fix(s.get("raw_away"), s["away"])
    rec = dict(
        source_odds="screenshot",
        has_result=0,
        home=s["home"], away=s["away"], stage="unknown",
        o_h=s["ft_h"], o_d=s["ft_d"], o_a=s["ft_a"],
        hg=None, ag=None, fr=None,
        shot_id=s["id"], shot_folder=s["folder"],
        shot_ft_1x2=(s["ft_h"], s["ft_d"], s["ft_a"]),
        shot_ft_ah=s["ft_ah"], shot_ft_ou=s["ft_ou"],
        shot_ht_1x2=(s["ht_h"], s["ht_d"], s["ht_a"]),
    )
    merged.append(rec)
    stat["shot_only"] += 1

# 写 CSV
cols = ["source_odds","has_result","home","away","stage",
        "o_h","o_d","o_a","hg","ag","fr",
        "shot_id","shot_folder","shot_ft_1x2","shot_ft_ah","shot_ft_ou","shot_ht_1x2"]
with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(cols)
    for r in merged:
        w.writerow([r.get(c) for c in cols])

with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump({"stat": stat, "name_fix": name_fix, "matches": merged},
              f, ensure_ascii=False, indent=1)

# 规范化校验
all_teams = sorted(set(r["home"] for r in merged) | set(r["away"] for r in merged))
print("=== 合并统计 ===")
for k, v in stat.items(): print(f"  {k}: {v}")
print(f"  合并总场数: {len(merged)}")
print(f"  有赛果(可用于监督训练): {sum(1 for r in merged if r['has_result'])}")
print(f"\n=== 队名修正记录 (原始->规范, 共 {len(name_fix)} 种) ===")
for raw, c in sorted(name_fix.items()): print(f"  {raw} -> {c}")
print(f"\n=== 规范化后去重队名数: {len(all_teams)} (预期 48 = 2026 世界杯 48 队) ===")
print("  队名:", all_teams)
print(f"\n输出:\n  {OUT_CSV}\n  {OUT_JSON}")
