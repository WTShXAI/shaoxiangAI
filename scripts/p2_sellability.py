"""P2-4 可售性评估 — 综合 P2-1(目录)/P2-2(字典)/P2-3(质量) 的资产打包与变现档

只读(消费既有报告)。把资产归并为可售产品包, 给变现档 + 合规前提 + 质量门槛。
输出:
  reports/p2_sellability.json
  reports/p2_sellability.md

用法: .venv/Scripts/python.exe scripts/p2_sellability.py
"""
import json, os
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "reports")
CAT = json.load(open(os.path.join(OUT, "p2_asset_catalog.json"), encoding="utf-8"))
QUAL = json.load(open(os.path.join(OUT, "p2_quality_report.json"), encoding="utf-8"))

REFERENCE = {  # 参考/派生层(公开, 低单价)
    ("football_data","team_canonical"),("football_data","teams"),
    ("football_data","handicap_labels"),("football_data","handicap_depth_profile"),
    ("football_data","stadiums"),("football_data","standings"),
    ("football_data","form_trends"),("football_data","upset_matches"),
    ("football_data","fd_competitions"),("football_data","betting_markets"),
    ("football_data","betfair_market"),("football_data","leisu_results"),
    ("football_data","weather_data"),("football_data","fd_matches"),
    ("football_data","wc_all_matches"),("football_data","wc_lineups"),
    ("football_data","wc_xlsx_matches"),("leisu_odds","live_scores"),
}
ODDS = {  # 赔率时间序列(须合规后售, 最高价值)
    ("events","odds_snapshots"),("events","odds_changes"),
    ("events","odds_snapshots_bak"),("events","odds_changes_bak"),
    ("gq","odds_snapshots"),("gq","odds_changes"),
    ("football_data","interwetten_odds"),("football_data","leisu_odds"),
    ("football_data","leisu_odds_legacy"),("football_data","live_odds_raw"),
    ("football_data","odds"),("football_data","odds_features"),
    ("football_data","odds_history"),("football_data","ou_live_feed"),
    ("football_data","ou_validation"),("football_data","ou_validation_local"),
    ("leisu_odds","odds_snapshots"),
}

def package_of(db, t):
    if (db, t) in ODDS:
        return "B_odds"
    if (db, t) == ("football_data","william_ht"):
        return "D_ht"
    if (db, t) in REFERENCE:
        return "C_ref"
    return "A_results"  # 其余可售表归赛果/主数据

PKG_META = {
 "A_results": ("Package A · 历史赛果主数据", "体育数据 API(公开赛果, 无PII, 直接可售)",
               "须过 clean_outcomes 假0-0 过滤; events.matches 已打标1,686假0-0"),
 "B_odds":   ("Package B · 庄家赔率时间序列", "博彩市场数据 API(价值最高, 须合规后售)",
               "再分发合规边界待 P2-5 确认; 含 William/Interwetten/乐鱼/雷速多源盘口"),
 "C_ref":    ("Package C · 参考/派生层", "增值参考(队名规范/盘口标签/积分/状态)",
               "公开派生, 无 PII, 可作免费层或捆绑增值"),
 "D_ht":     ("Package D · 真实半场子集", "半场比分+半场盘口子集(须脱敏盘口部分)",
               "william_ht 45.8万中仅 61.3% 真实半场(ht<ft); 比分部分可分离为可售"),
}

def main():
    pkgs = {k: {"tables": [], "rows": 0, "items": []} for k in PKG_META}
    for a in CAT["assets"]:
        if a["category"] == "INTERNAL":
            continue
        if a["db"] == "football_data" and a["table"] == "users":
            continue  # 密钥, 严禁
        p = package_of(a["db"], a["table"])
        pkgs[p]["tables"].append(f"{a['db']}.{a['table']}")
        if a["rows"]:
            pkgs[p]["rows"] += a["rows"]
        pkgs[p]["items"].append({"t": f"{a['db']}.{a['table']}", "rows": a["rows"],
                                 "value": a["value"]})

    rep = {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
           "packages": {}}
    for k, meta in PKG_META.items():
        rep["packages"][k] = {"name": meta[0], "monetization": meta[1],
                              "prereq": meta[2], "rows": pkgs[k]["rows"],
                              "table_count": len(pkgs[k]["tables"]),
                              "tables": pkgs[k]["tables"]}

    # 顶层结论
    q = QUAL["sections"]
    rep["verdict"] = {
        "directly_sellable_now": "Package A(赛果,过clean) + Package C(参考) — 无合规硬障碍, 可立即打包",
        "highest_value_gated": f"Package B 赔率时间序列 ≈ {pkgs['B_odds']['rows']:,} 行 — 价值最高但须 P2-5 合规放行",
        "quality_gate": f"events.matches 假0-0 已治理(打标{pkgs and QUAL['sections']['events_matches_fake00']['score_missing_tagged']:,}场); historical_matches 无打标须补",
        "blocker_removed": "gq.matches 联赛缺失实测0.1% → 分联赛切片可行(早期98%缺league记忆已过时)",
        "never_ship": "football_data.users(password_hash) 严禁外发",
        "valuation_implication": "P2 数据资产独立变现价值成立(尤其 Package B); 支撑路线图 P2 区间 ¥85–110万 下限位",
    }

    jp = os.path.join(OUT, "p2_sellability.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    md = ["# 哨响AI 数据可售性评估（P2-4）\n",
          f"> 生成: {rep['generated_at']} | 综合 P2-1/2/3\n"]
    for k, meta in PKG_META.items():
        p = pkgs[k]
        md.append(f"\n## {meta[0]}  [{len(p['tables'])} 表 / {p['rows']:,} 行]\n")
        md.append(f"- 变现形态: {meta[1]}")
        md.append(f"- 前提/门槛: {meta[2]}")
    md.append("\n## 顶层结论\n")
    for k, v in rep["verdict"].items():
        md.append(f"- **{k}**: {v}")
    mp = os.path.join(OUT, "p2_sellability.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"[p2_sellability] 完成 -> {jp}\n  -> {mp}")


if __name__ == "__main__":
    main()
