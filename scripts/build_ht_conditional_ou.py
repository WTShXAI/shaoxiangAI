"""
哨响AI · HT 条件化 OU 偏差表 (build_ht_conditional_ou)
=====================================================
目的：把 OU 校准从"无条件(只看盘口线)"升级为"条件化(看半场比分状态)"。
        用户(涛哥)实证：HT 1:1 + 大球3.5 会命中 —— 这是结构性 open-game 信号。
数据源：football_data.db.matches (含 halftime_home/away + 终场比分, 1829 场)
输出：data/ht_conditional_ou.json
      结构: { ht:"1:1", n:171, lines:{ "3.5":{over_rate:0.45, unconditional:0.35, gap_pp:+10.0} } }
说明：matches 无历史 OU 赔率, 故只算"真实大球率 by HT", 与全局无条件率对比得 gap
      (gap>0 表示该 HT 状态下大球被庄家无条件定价低估 → 大球值博)。
"""

import sqlite3
import json
import statistics
from collections import defaultdict

DB = r"D:\Architecture\data\football_data.db"
OUT = r"D:\Architecture\data\ht_conditional_ou.json"
LINES = [1.5, 2.5, 3.5, 4.5]
MIN_HT_N = 15


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""SELECT halftime_home, halftime_away, home_score, away_score
                   FROM matches
                   WHERE halftime_home IS NOT NULL AND halftime_away IS NOT NULL
                     AND home_score IS NOT NULL AND away_score IS NOT NULL""")
    rows = cur.fetchall()
    con.close()

    # 全局无条件大球率(基线)
    all_totals = [int(h) + int(a) for _, _, h, a in rows]
    unconditional = {L: sum(1 for t in all_totals if t > L) / len(all_totals) for L in LINES}

    # 按 HT 状态分组的总进球
    by_ht = defaultdict(list)
    for hha, haa, hsa, asa in rows:
        ht = f"{int(hha)}:{int(haa)}"
        by_ht[ht].append(int(hsa) + int(asa))

    result = {"unconditional_over_rate": {str(L): round(unconditional[L], 4) for L in LINES},
              "ht_states": []}

    for ht, totals in sorted(by_ht.items(), key=lambda kv: -len(kv[1])):
        n = len(totals)
        if n < MIN_HT_N:
            continue
        entry = {"ht": ht, "n": n, "lines": {}}
        for L in LINES:
            over = sum(1 for t in totals if t > L) / n
            gap = (over - unconditional[L]) * 100
            entry["lines"][str(L)] = {
                "over_rate": round(over, 4),
                "unconditional": round(unconditional[L], 4),
                "gap_pp": round(gap, 2),
            }
        result["ht_states"].append(entry)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 控制台速览
    print(f"样本: {len(rows)} | 全局无条件大球率: " +
          ", ".join(f"{L}:{unconditional[L]*100:.1f}%" for L in LINES))
    print(f"\n{'HT':>5}{'n':>6}  " + "  ".join(f"{L}线大球率/gap" for L in LINES))
    for e in result["ht_states"]:
        cells = []
        for L in LINES:
            c = e["lines"][str(L)]
            cells.append(f"{c['over_rate']*100:.0f}%(+{c['gap_pp']:.0f})")
        print(f"{e['ht']:>5}{e['n']:>6}  " + "  ".join(f"{L}:{c}" for L, c in zip(LINES, cells)))
    print(f"\n[已落盘] -> {OUT}")


if __name__ == "__main__":
    main()
