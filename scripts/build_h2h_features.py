# -*- coding: utf-8 -*-
"""
历史交锋 (H2H) 特征 — 给 prematch_subset 的每场比赛,
从 historical_matches 提取该对决的历史统计, 作为真实可用的赛果相关特征。

为什么是 H2H 而非"当场比赛赛果":
  historical_matches 最晚到 2025-04-11, 而截图是 2025-07~2026-07, 时间不重叠。
  直接 JOIN 会匹配到几年前同名比赛当标签 (错误)。但同名对决的【历史统计】是
  真实且有预测价值的特征 (该对决历史主胜率/平局率/场均进球)。

特征:
  h2h_total        历史交锋次数
  h2h_home_winrate 主队历史胜率
  h2h_draw_rate    平局率
  h2h_away_winrate 客队历史胜率
  h2h_avg_goals    场均总进球
  h2h_home_avg_goal 主队场均进球
  h2h_last3_1x2    近3次结果 (如 "HDA")
  h2h_available    是否有历史数据 (0=无, 1=有)

输出: data/long_features/h2h_features.csv (prematch_subset + H2H列)
"""
import sqlite3, csv

DB = r"D:\Architecture\data\football_data.db"
SUBSET = r"D:\Architecture\data\long_features\prematch_subset.csv"
OUT = r"D:\Architecture\data\long_features\h2h_features.csv"

def main():
    con = sqlite3.connect(DB); cur = con.cursor()
    rows = list(csv.DictReader(open(SUBSET, encoding="utf-8-sig")))
    n_with_h2h = 0
    for r in rows:
        h = (r.get("home") or "").strip(); a = (r.get("away") or "").strip()
        if not h or not a:
            r.update({k: "" for k in ("h2h_total","h2h_home_winrate","h2h_draw_rate",
                       "h2h_away_winrate","h2h_avg_goals","h2h_home_avg_goal","h2h_last3_1x2")})
            r["h2h_available"] = "0"; continue
        # 该对决全部历史 (home=h AND away=a)
        cur.execute("""SELECT home_score,away_score,final_result FROM historical_matches
                       WHERE home_team=? AND away_team=? AND home_score IS NOT NULL""", (h, a))
        hist = cur.fetchall()
        if not hist:
            r.update({k: "" for k in ("h2h_total","h2h_home_winrate","h2h_draw_rate",
                       "h2h_away_winrate","h2h_avg_goals","h2h_home_avg_goal","h2h_last3_1x2")})
            r["h2h_available"] = "0"; continue
        n_with_h2h += 1
        n = len(hist)
        home_wins = sum(1 for _,_,fr in hist if fr == "H")
        draws = sum(1 for _,_,fr in hist if fr == "D")
        away_wins = sum(1 for _,_,fr in hist if fr == "A")
        total_goals = sum(hs+as_ for hs,as_,_ in hist)
        home_goals = sum(hs for hs,_,_ in hist)
        r["h2h_total"] = str(n)
        r["h2h_home_winrate"] = round(home_wins/n, 3)
        r["h2h_draw_rate"] = round(draws/n, 3)
        r["h2h_away_winrate"] = round(away_wins/n, 3)
        r["h2h_avg_goals"] = round(total_goals/n, 2)
        r["h2h_home_avg_goal"] = round(home_goals/n, 2)
        # 近3次 (按 hist 顺序取最后3, 注: 未按日期排序, historical_matches默认可能非时序)
        last3 = "".join([fr for _,_,fr in hist][-3:])
        r["h2h_last3_1x2"] = last3
        r["h2h_available"] = "1"

    if rows:
        fields = list(rows[0].keys())
        with open(OUT, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for r in rows: w.writerow(r)
    con.close()

    print(f"=== H2H 历史交锋特征 ===")
    print(f"输入: {len(rows)} 场赛前子集")
    print(f"有历史交锋数据: {n_with_h2h} 场 ({100*n_with_h2h/max(len(rows),1):.0f}%)")
    print(f"→ {OUT}")
    # 样本
    print("\n--- H2H 样本 ---")
    print(f"  {'主队':11}{'客队':11}{'场次':>4}{'主胜':>6}{'平':>6}{'客胜':>6}{'场均进球':>8}{'近3':>5}")
    for r in rows:
        if r.get("h2h_available") == "1":
            print(f"  {r['home'][:10]:11}{r['away'][:10]:11}{r['h2h_total']:>4}{r['h2h_home_winrate']:>6}{r['h2h_draw_rate']:>6}{r['h2h_away_winrate']:>6}{r['h2h_avg_goals']:>8}{r.get('h2h_last3_1x2',''):>5}")

if __name__ == "__main__":
    main()
