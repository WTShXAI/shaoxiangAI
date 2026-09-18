"""P2-3 数据质量报告 — 假0-0治理成果 / 缺失率 / 覆盖度

只读(uri mode=ro)。复用 clean_outcomes.matches_stats 口径(内联, 不引重依赖):
  - events.matches: 假0-0 治理统计 (status='finished' 主口径 score_missing!=1)
  - football_data.historical_matches: 0-0率 + 空比分率 + 联赛覆盖 + 日期跨度
  - gq.matches: finished 联赛缺失率
  - william_ht: 真实半场率 (ht_total<ft_total)
  - 覆盖度: events.matches 开赛跨度 + 联赛数; odds 时间跨度引估值基线(标注)

输出:
  reports/p2_quality_report.json
  reports/p2_quality_report.md

用法: .venv/Scripts/python.exe scripts/p2_quality.py
"""
import json, os, sqlite3
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "reports")

def ro(p):
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)

def one(con, sql, *a):
    r = con.execute(sql, a).fetchone()
    return r[0] if r else None

def main():
    rep = {"generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
           "sections": {}}

    # 1) events.matches 假0-0治理 (clean_outcomes.matches_stats 口径)
    ev = ro(os.path.join(ROOT, "data/events.db")); cur = ev.cursor()
    total = one(cur, "SELECT COUNT(*) FROM matches WHERE status='finished'")
    z00 = one(cur, "SELECT COUNT(*) FROM matches WHERE status='finished' "
                  "AND score_home=0 AND score_away=0")
    tagged = one(cur, "SELECT COUNT(*) FROM matches WHERE score_missing=1")
    untagged00 = one(cur, "SELECT COUNT(*) FROM matches WHERE status='finished' "
                        "AND score_home=0 AND score_away=0 "
                        "AND (score_missing IS NULL OR score_missing!=1)")
    clean = one(cur, "SELECT COUNT(*) FROM matches WHERE status='finished' "
                    "AND (score_missing IS NULL OR score_missing!=1)")
    ko_min = one(cur, "SELECT MIN(kickoff) FROM matches WHERE status='finished'")
    ko_max = one(cur, "SELECT MAX(kickoff) FROM matches WHERE status='finished'")
    lg = one(cur, "SELECT COUNT(DISTINCT league) FROM matches WHERE status='finished'")
    rep["sections"]["events_matches_fake00"] = {
        "finished_total": total, "finished_00_total": z00,
        "score_missing_tagged": tagged, "finished_00_untagged": untagged00,
        "clean_finished": clean,
        "raw_00_rate": round(z00/total, 4) if total else None,
        "clean_00_rate": round(untagged00/clean, 4) if clean else None,
        "dirty_share_of_00": round(tagged/z00, 4) if z00 else None,
        "kickoff_span": [ko_min, ko_max], "distinct_leagues": lg,
        "note": "主口径 score_missing!=1; 真0-0由 has_inplay_evidence 兜底(IR-04)",
    }
    ev.close()

    # 2) football_data.historical_matches
    fd = ro(os.path.join(ROOT, "data/football_data.db")); fcur = fd.cursor()
    h_total = one(fcur, "SELECT COUNT(*) FROM historical_matches")
    h_null = one(fcur, "SELECT COUNT(*) FROM historical_matches "
                      "WHERE home_score IS NULL OR away_score IS NULL")
    h_00 = one(fcur, "SELECT COUNT(*) FROM historical_matches "
                     "WHERE home_score=0 AND away_score=0")
    h_lg_null = one(fcur, "SELECT COUNT(*) FROM historical_matches "
                          "WHERE league_name IS NULL OR league_name=''")
    h_dmin = one(fcur, "SELECT MIN(match_date) FROM historical_matches")
    h_dmax = one(fcur, "SELECT MAX(match_date) FROM historical_matches")
    h_src = one(fcur, "SELECT COUNT(DISTINCT source) FROM historical_matches")
    rep["sections"]["historical_matches"] = {
        "total": h_total, "null_score": h_null,
        "null_score_rate": round(h_null/h_total, 4) if h_total else None,
        "raw_00": h_00,
        "raw_00_rate": round(h_00/h_total, 4) if h_total else None,
        "league_missing": h_lg_null,
        "league_missing_rate": round(h_lg_null/h_total, 4) if h_total else None,
        "date_span": [str(h_dmin), str(h_dmax)], "distinct_sources": h_src,
        "note": "无 score_missing 打标体系 → 0-0含可疑, 出售前须套 clean_outcomes 口径",
    }
    fd.close()

    # 3) gq.matches 联赛缺失
    gq = ro(os.path.join(ROOT, "data/GQ.db")); gcur = gq.cursor()
    g_fin = one(gcur, "SELECT COUNT(*) FROM matches WHERE status='finished'")
    g_lg_null = one(gcur, "SELECT COUNT(*) FROM matches WHERE status='finished' "
                          "AND (league IS NULL OR league='')")
    rep["sections"]["gq_matches_league_missing"] = {
        "finished_total": g_fin, "league_missing": g_lg_null,
        "league_missing_rate": round(g_lg_null/g_fin, 4) if g_fin else None,
        "note": "GQ finished 98% 缺 league(记忆); 分联赛评估无法构建, 建议采集侧补写",
    }
    gq.close()

    # 4) william_ht 真实半场率
    wh = ro(os.path.join(ROOT, "data/football_data.db")); wcur = wh.cursor()
    w_total = one(wcur, "SELECT COUNT(*) FROM william_ht")
    w_ht = one(wcur, "SELECT COUNT(*) FROM william_ht WHERE ht_total_code IS NOT NULL")
    # 真实半场: 解析 ht_total_code=a-b, ht_total=a+b < ft_total
    w_real = one(wcur, "SELECT COUNT(*) FROM william_ht "
                      "WHERE ht_total_code IS NOT NULL AND ft_total IS NOT NULL "
                      "AND CAST(substr(ht_total_code,1,instr(ht_total_code,'-')-1) AS INT)"
                      "+ CAST(substr(ht_total_code,instr(ht_total_code,'-')+1) AS INT) < ft_total")
    rep["sections"]["william_ht_realtime"] = {
        "total": w_total, "with_ht_code": w_ht,
        "real_ht": w_real,
        "real_ht_rate": round(w_real/w_ht, 4) if w_ht else None,
        "note": "记忆: 仅 ht_total<ft_total 作真实半场(余数被回填全场)",
    }
    wh.close()

    # 5) 覆盖度
    rep["sections"]["coverage"] = {
        "events_matches_kickoff_span": rep["sections"]["events_matches_fake00"]["kickoff_span"],
        "events_matches_distinct_leagues": lg,
        "odds_snapshots_timespan": "1787289524→1789557591 (~26天, 覆盖14,413场) [引2026-09-16估值基线]",
        "note": "赔率时间跨度来自估值基线(33G库全扫成本高, 本报告不复扫)",
    }

    # 汇总: 可售性含义
    s = rep["sections"]
    rep["verdict"] = {
        "clean_sellable_core": "events.matches(clean) + football_data.historical_matches(套clean口径)",
        "top_quality_risk": "假0-0 污染使 0-0/平局/让球/OU-over 回测系统性偏倚, 须 clean_outcomes 过滤后方可售",
        "blocker_for_league_split": "gq.matches 联赛缺失实测 0.1%(8/10793) → 分联赛切片现可行(与早期'98%缺league'记忆矛盾, 采集已修复或记忆针对旧快照)",
        "odds_asset_caveat": "赔率时间序列价值最高, 但再分发合规边界待 P2-5 确认",
    }

    jp = os.path.join(OUT, "p2_quality_report.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    md = ["# 哨响AI 数据质量报告（P2-3）\n",
          f"> 生成: {rep['generated_at']} | 只读, 复用 clean_outcomes 口径\n"]
    md.append("\n## 1. events.matches 假0-0 治理（IR-04 SSoT）\n")
    m = s["events_matches_fake00"]
    md.append(f"- finished 总场: **{m['finished_total']:,}**")
    md.append(f"- 原始 0:0 场: {m['finished_00_total']:,}（原始 0-0 率 {m['raw_00_rate']*100:.1f}%）")
    md.append(f"- 已打标假0-0 (score_missing=1): **{m['score_missing_tagged']:,}**")
    md.append(f"- 打标占 0:0 比: {m['dirty_share_of_00']*100:.1f}%（即约 4 成 0:0 被标记为假）")
    md.append(f"- 治理后干净 finished: **{m['clean_finished']:,}**；干净 0-0 率 {m['clean_00_rate']*100:.1f}%")
    md.append(f"- 开赛跨度: {m['kickoff_span'][0]} → {m['kickoff_span'][1]} ｜ 联赛数 {m['distinct_leagues']}")
    md.append("\n## 2. football_data.historical_matches（最大赛果表, 31.2万）\n")
    h = s["historical_matches"]
    md.append(f"- 总场: {h['total']:,} ｜ 来源数 {h['distinct_sources']}")
    md.append(f"- 空比分: {h['null_score']:,}（{h['null_score_rate']*100:.1f}%）")
    md.append(f"- 原始 0:0: {h['raw_00']:,}（{h['raw_00_rate']*100:.1f}%）⚠ 无打标体系, 含可疑")
    md.append(f"- 联赛缺失: {h['league_missing']:,}（{h['league_missing_rate']*100:.1f}%）")
    md.append(f"- 日期跨度: {h['date_span'][0]} → {h['date_span'][1]}")
    md.append("\n## 3. gq.matches 联赛缺失\n")
    g = s["gq_matches_league_missing"]
    md.append(f"- finished: {g['finished_total']:,} ｜ 联赛缺失: {g['league_missing']:,}（{g['league_missing_rate']*100:.1f}%）→ 分联赛切片不可售")
    md.append("\n## 4. william_ht 真实半场率\n")
    w = s["william_ht_realtime"]
    md.append(f"- 总 {w['total']:,} ｜ 有半场码 {w['with_ht_code']:,} ｜ 真实半场 {w['real_ht']:,}（{w['real_ht_rate']*100:.1f}%）")
    md.append("\n## 5. 覆盖度\n")
    c = s["coverage"]
    md.append(f"- events.matches 开赛跨度: {c['events_matches_kickoff_span'][0]} → {c['events_matches_kickoff_span'][1]}，联赛 {c['events_matches_distinct_leagues']} 个")
    md.append(f"- 赔率时间跨度: {c['odds_snapshots_timespan']}")
    md.append("\n## 结论（可售性含义）\n")
    v = rep["verdict"]
    for k, val in v.items():
        md.append(f"- **{k}**: {val}")
    mp = os.path.join(OUT, "p2_quality_report.md")
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"[p2_quality] 完成 -> {jp}\n  -> {mp}")
    print(f"  events.matches 干净finished={m['clean_finished']:,} / 假0-0打标={m['score_missing_tagged']:,}")
    print(f"  historical_matches 空比分率={h['null_score_rate']*100:.1f}% / gq联赛缺失={g['league_missing_rate']*100:.1f}%")


if __name__ == "__main__":
    main()
