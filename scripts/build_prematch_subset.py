# -*- coding: utf-8 -*-
"""
赛前口径子集 — 从滚球特征表里, 按 (home,away,league) 聚合, 每场取【最早时间点】的盘口
作为赛前近似, 用于喂赛前口径模型 (score_model / draw_signal)。

逻辑:
  - 同一比赛 (home+away+league 相同) 多条滚球记录 → 取 date 最小的那条
  - 优先取有独赢三赔 (odds_h) 的, 其次取 status 为"即将开赛/未开场"的
  - 输出 prematch_subset.csv

⚠️ 局限: 这是"该数据集中该场最早截图", 未必是真正的临场赛前终盘。
   若一场只在滚球阶段被截过图, 则该子集仍含赛中比分 (score_home_live)。
   脚本会标注 is_truly_prematch (status 含即将开赛/未开场 且无 live 比分)。
"""
import csv, os
from collections import defaultdict

IN_CSV = r"D:\Architecture\data\long_features\match_features_canon.csv"
OUT_CSV = r"D:\Architecture\data\long_features\prematch_subset.csv"

def norm(s):
    return (s or "").strip().lower()

def main():
    rows = list(csv.DictReader(open(IN_CSV, encoding="utf-8-sig")))
    # 只取盘口数据 (非资金面), 且有队名
    odds_rows = [r for r in rows if r.get("data_type") == "live_odds" and r.get("home") and r.get("away")]

    # 按 (home,away,league) 分组
    groups = defaultdict(list)
    for r in odds_rows:
        key = (norm(r["home"]), norm(r["away"]), norm(r.get("league", "")))
        groups[key].append(r)

    subset = []
    for key, recs in groups.items():
        # 排序: ① 优先有 odds_h 的; ② 优先即将开赛/未开场; ③ date 升序(最早)
        def sort_key(r):
            has_1x2 = 0 if r.get("odds_h") else 1
            is_prematch_status = 0 if r.get("status", "") in ("即将开赛", "未开场") else 1
            return (has_1x2, is_prematch_status, r.get("date", ""))
        recs_sorted = sorted(recs, key=sort_key)
        best = recs_sorted[0]
        # 标注是否真赛前 (无 live 比分 + status 为即将开赛/未开场)
        has_live_score = bool(best.get("score_home_live") or best.get("score_away_live"))
        is_pre = best.get("status", "") in ("即将开赛", "未开场", "") and not has_live_score
        best["is_truly_prematch"] = "1" if is_pre else "0"
        best["n_snapshots"] = str(len(recs))  # 该场共几张截图
        subset.append(best)

    if not subset:
        print("无可用盘口数据"); return
    fields = list(subset[0].keys())
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in subset: w.writerow(r)

    n_with_1x2 = sum(1 for r in subset if r.get("odds_h"))
    n_truly_pre = sum(1 for r in subset if r.get("is_truly_prematch") == "1")
    n_multi = sum(1 for r in subset if int(r.get("n_snapshots", "1")) > 1)
    print(f"=== 赛前口径子集 ===")
    print(f"独立比赛数: {len(subset)} (从 {len(odds_rows)} 条滚球记录聚合)")
    print(f"  其中有独赢三赔(可直接喂score_model): {n_with_1x2}")
    print(f"  其中真赛前(无live比分+即将开赛): {n_truly_pre}")
    print(f"  其中有多张截图(可算赔率波动): {n_multi}")
    print(f"→ {OUT_CSV}")
    # 抽样
    print("\n--- 子集样本 ---")
    for r in subset[:8]:
        oh = r.get("odds_h", "")
        print(f"  {r.get('home','')[:10]:10} vs {r.get('away','')[:10]:10} | {r.get('date','')[:16]} | 独={oh or '无'}/{r.get('odds_d','')}/{r.get('odds_a','')} | live比={r.get('score_home_live','-')}:{r.get('score_away_live','-')} | 真赛前={r.get('is_truly_prematch')} | 截图数={r.get('n_snapshots')}")

if __name__ == "__main__":
    main()
