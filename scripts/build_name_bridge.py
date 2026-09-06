# -*- coding: utf-8 -*-
"""
步骤1+2合并: 全量监督宽表构建 (数据挖透核心)
================================================
侦察发现: training_extended ↔ william_ht 三方JOIN(日期+主+客)命中率93.1%,
无需额外队名归一化工程。直接建宽表。

宽表结构 (以 training_extended 为底, LEFT JOIN william_ht + handicap_labels):
  L1 标签: final_result(1X2), result_class, total_goals, ht_label(半场赛果)
  L2 赔率: open/close 1X2 + drift + overround + imp_prob (training_extended自带)
  L3 半场: ht_home/ht_away/ht_total_code (来自william_ht, 填补te的空缺)
  L4 让球: 暂不(join率低, 单独表)

输出:
  data/master_dataset.csv (全量, ~31万行)
  data/master_dataset_joined.csv (带半场的子集, ~18.7万行, OU验证用)
  data/master_dataset_report.json (质量报告)
"""
import sqlite3, csv, json, os

DB = r"D:\Architecture\data\football_data.db"
OUT_FULL = r"D:\Architecture\data\master_dataset.csv"
OUT_JOINED = r"D:\Architecture\data\master_dataset_joined.csv"
REPORT = r"D:\Architecture\data\master_dataset_report.json"

def main():
    con = sqlite3.connect(DB); cur = con.cursor()

    # ══ 宽表SQL: training_extended LEFT JOIN william_ht ══
    # 选关键列, 避免列过多
    sql_joined = """
    SELECT
      t.match_date, t.league_name, t.home_team, t.away_team,
      t.home_score, t.away_score, t.final_result, t.result_class,
      (t.home_score + t.away_score) AS te_total_goals,
      t.odds_home, t.odds_draw, t.odds_away,
      t.open_home, t.open_draw, t.open_away,
      t.odds_imp_h, t.odds_imp_d, t.odds_imp_a,
      t.odds_spread, t.odds_overround, t.odds_draw_dev,
      t.drift_h, t.drift_d, t.drift_a, t.drift_magnitude, t.drift_direction,
      t.otsm_lock_confidence, t.otsm_water_accel,
      -- william_ht 半场+总进球 (填补te空缺)
      w.ht_home, w.ht_away, w.ht_total_code, w.label AS ht_label,
      w.ft_total, w.open_home_odds AS wh_open_h, w.open_draw_odds AS wh_open_d, w.open_away_odds AS wh_open_a
    FROM training_extended t
    LEFT JOIN william_ht w
      ON t.match_date = w.match_date
     AND t.home_team = w.home_team_norm
     AND t.away_team = w.away_team_norm
    WHERE t.final_result IS NOT NULL
    ORDER BY t.match_date
    """

    print("构建全量宽表 (training_extended LEFT JOIN william_ht)...")
    cur.execute(sql_joined)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print(f"  总行数: {len(rows)}, 列数: {len(cols)}")

    # 写全量CSV
    with open(OUT_FULL, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(cols)
        w.writerows(rows)
    print(f"  → {OUT_FULL}")

    # 统计质量
    n = len(rows)
    ht_label_idx = cols.index("ht_label")
    ht_total_idx = cols.index("ht_total_code")
    ft_total_idx = cols.index("ft_total")
    wh_idx = cols.index("wh_open_h")
    has_ht = sum(1 for r in rows if r[ht_label_idx] is not None)
    has_ft_total = sum(1 for r in rows if r[ft_total_idx] is not None)
    has_wh = sum(1 for r in rows if r[wh_idx] is not None)

    # result_class 分布
    rc_idx = cols.index("result_class")
    from collections import Counter
    rc_dist = Counter(r[rc_idx] for r in rows)
    # ht_label 分布 (有半场的子集)
    ht_dist = Counter(r[ht_label_idx] for r in rows if r[ht_label_idx] is not None)
    # 时间分布
    date_idx = cols.index("match_date")
    years = Counter(str(r[date_idx])[:4] for r in rows)

    report = {
        "总行数": n,
        "列数": len(cols),
        "字段": cols,
        "result_class分布(1X2标签)": dict(rc_dist),
        "半场label覆盖": {f"有半场赛果": has_ht, f"覆盖率": f"{100*has_ht//n}%"},
        "ht_label分布(0=主胜/1=平/2=客胜)": dict(ht_dist),
        "全场总进球覆盖": {f"有ft_total": has_ft_total, f"覆盖率": f"{100*has_ft_total//n}%"},
        "william_hill开盘赔率覆盖": {f"有wh_open": has_wh, f"覆盖率": f"{100*has_wh//n}%"},
        "时间分布(按年)": dict(sorted(years.items())),
        "用途": {
            "全量(31万行)": "1X2分类训练 + drift/otsm特征 + 波胆校准",
            "半场子集(18.7万行)": "OU/总进球验证 + 半场赛果预测 (步骤3核心)",
        },
    }
    json.dump(report, open(REPORT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"  → {REPORT}")
    print(f"\n=== 质量摘要 ===")
    print(f"  1X2标签分布: {dict(rc_dist)}")
    print(f"  半场赛果覆盖: {has_ht}/{n} ({100*has_ht//n}%)")
    print(f"  全场总进球覆盖: {has_ft_total}/{n} ({100*has_ft_total//n}%)")
    con.close()

if __name__ == "__main__":
    main()
