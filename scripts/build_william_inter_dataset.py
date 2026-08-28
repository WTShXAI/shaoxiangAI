#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_william_inter_dataset.py
--------------------------------
提取 football_data.db 中 威廉(william_ht) 与 Inter(interwetten_odds) 的历史盘口+赛果，
统一成一份带标签的训练集，供"新模型"使用。

"正确选项用红色标注" -> 在本数据集里等价于把正确结果编码进标签列:
  - result_class : 0=主胜(H) / 1=平(D) / 2=客胜(A)   <- 1X2 正确选项
  - home_score/away_score/total_goals                 <- 终场比分正确选项
  - ht_result_class (仅威廉) : 0/1/2                   <- 半场比分正确选项

特征全部由盘口派生(开盘/收盘 1X2 + 派生量)，不含任何未来信息，可做严格时序切分。

输出: data/william_inter_training.csv
"""
import sqlite3, math, os, sys

DB = "data/football_data.db"
OUT = "data/william_inter_training.csv"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm_league(s):
    if s is None:
        return ""
    return " ".join(str(s).split())  # 去首尾及中间多余空白 (威廉联赛名带尾随空格)


def derive(open_h, open_d, open_a, close_h, close_d, close_a):
    """由盘口派生特征。返回 dict，缺值安全。"""
    feats = {}
    oh, od, oa, ch, cd, ca = open_h, open_d, open_a, close_h, close_d, close_a
    # 收盘隐含概率
    try:
        cor = (1.0 / ch + 1.0 / cd + 1.0 / ca)
        ih, id_, ia = (1.0 / ch) / cor, (1.0 / cd) / cor, (1.0 / ca) / cor
    except Exception:
        cor, ih, id_, ia = float("nan"), float("nan"), float("nan"), float("nan")
    # 开盘隐含概率
    try:
        oor = (1.0 / oh + 1.0 / od + 1.0 / oa)
        oih, oid, oia = (1.0 / oh) / oor, (1.0 / od) / oor, (1.0 / oa) / oor
    except Exception:
        oor, oih, oid, oia = float("nan"), float("nan"), float("nan"), float("nan")
    feats["close_overround"] = cor
    feats["imp_h"], feats["imp_d"], feats["imp_a"] = ih, id_, ia
    feats["open_overround"] = oor
    feats["imp_open_h"], feats["imp_open_d"], feats["imp_open_a"] = oih, oid, oia
    # 赔率漂移 (收盘/开盘 - 1); >0 表示该方向赔率上升(更不被看好)
    try:
        feats["drift_h"] = ch / oh - 1.0
        feats["drift_d"] = cd / od - 1.0
        feats["drift_a"] = ca / oa - 1.0
    except Exception:
        feats["drift_h"] = feats["drift_d"] = feats["drift_a"] = float("nan")
    # 主客相对 + 平局相对
    try:
        feats["ha_ratio"] = ch / ca
    except Exception:
        feats["ha_ratio"] = float("nan")
    try:
        feats["draw_ratio"] = cd / ((ch + ca) / 2.0)
    except Exception:
        feats["draw_ratio"] = float("nan")
    # 热门隐含概率(三类中最大)
    try:
        feats["fav_implied"] = max(ih, id_, ia)
    except Exception:
        feats["fav_implied"] = float("nan")
    return feats


def result_class_from_scores(h, a):
    if h is None or a is None:
        return None
    if h > a:
        return 0
    if h < a:
        return 2
    return 1


def main():
    c = sqlite3.connect(os.path.join(PROJECT_ROOT, DB))
    c.row_factory = sqlite3.Row
    rows = []

    # ---- 威廉 william_ht ----
    for r in c.execute("""
        SELECT match_id, match_date, league_name, home_team, away_team,
               ht_home, ht_away, h_ft, a_ft,
               open_home_odds, open_draw_odds, open_away_odds,
               close_home_odds, close_draw_odds, close_away_odds,
               home_team_norm, away_team_norm
        FROM william_ht
        WHERE close_home_odds IS NOT NULL AND close_draw_odds IS NOT NULL AND close_away_odds IS NOT NULL
          AND h_ft IS NOT NULL AND a_ft IS NOT NULL
    """):
        f = derive(r["open_home_odds"], r["open_draw_odds"], r["open_away_odds"],
                   r["close_home_odds"], r["close_draw_odds"], r["close_away_odds"])
        rc = result_class_from_scores(r["h_ft"], r["a_ft"])
        ht_h, ht_a = r["ht_home"], r["ht_away"]
        ht_rc = result_class_from_scores(ht_h, ht_a) if (ht_h is not None and ht_a is not None) else None
        rows.append({
            "source": "william",
            "src_match_id": r["match_id"],
            "match_date": r["match_date"],
            "league_name": norm_league(r["league_name"]),
            "home_team": r["home_team"], "away_team": r["away_team"],
            "home_team_norm": r["home_team_norm"], "away_team_norm": r["away_team_norm"],
            "open_h": r["open_home_odds"], "open_d": r["open_draw_odds"], "open_a": r["open_away_odds"],
            "close_h": r["close_home_odds"], "close_d": r["close_draw_odds"], "close_a": r["close_away_odds"],
            "home_score": r["h_ft"], "away_score": r["a_ft"],
            "total_goals": (r["h_ft"] or 0) + (r["a_ft"] or 0),
            "final_result": {0: "H", 1: "D", 2: "A"}[rc],
            "result_class": rc,
            "ht_home_score": ht_h, "ht_away_score": ht_a,
            "ht_total": (ht_h + ht_a) if ht_h is not None else None,
            "ht_result": ({0: "H", 1: "D", 2: "A"}.get(ht_rc) if ht_rc is not None else None),
            "ht_result_class": ht_rc,
            **f,
        })

    # ---- Inter interwetten_odds ----
    for r in c.execute("""
        SELECT hist_id, match_date, league_name, home_team, away_team,
               home_score, away_score, final_result, total_goals,
               open_home_odds, open_draw_odds, open_away_odds,
               close_home_odds, close_draw_odds, close_away_odds,
               home_team_norm, away_team_norm
        FROM interwetten_odds
        WHERE close_home_odds IS NOT NULL AND close_draw_odds IS NOT NULL AND close_away_odds IS NOT NULL
          AND home_score IS NOT NULL AND away_score IS NOT NULL
    """):
        f = derive(r["open_home_odds"], r["open_draw_odds"], r["open_away_odds"],
                   r["close_home_odds"], r["close_draw_odds"], r["close_away_odds"])
        rc = {"H": 0, "D": 1, "A": 2}.get(r["final_result"])
        if rc is None:
            rc = result_class_from_scores(r["home_score"], r["away_score"])
        rows.append({
            "source": "inter",
            "src_match_id": r["hist_id"],
            "match_date": r["match_date"],
            "league_name": norm_league(r["league_name"]),
            "home_team": r["home_team"], "away_team": r["away_team"],
            "home_team_norm": r["home_team_norm"], "away_team_norm": r["away_team_norm"],
            "open_h": r["open_home_odds"], "open_d": r["open_draw_odds"], "open_a": r["open_away_odds"],
            "close_h": r["close_home_odds"], "close_d": r["close_draw_odds"], "close_a": r["close_away_odds"],
            "home_score": r["home_score"], "away_score": r["away_score"],
            "total_goals": r["total_goals"] if r["total_goals"] is not None else (r["home_score"] + r["away_score"]),
            "final_result": {0: "H", 1: "D", 2: "A"}[rc],
            "result_class": rc,
            "ht_home_score": None, "ht_away_score": None,
            "ht_total": None, "ht_result": None, "ht_result_class": None,
            **f,
        })
    c.close()

    import csv
    cols = ["source", "src_match_id", "match_date", "league_name",
            "home_team", "away_team", "home_team_norm", "away_team_norm",
            "open_h", "open_d", "open_a", "close_h", "close_d", "close_a",
            "close_overround", "imp_h", "imp_d", "imp_a",
            "open_overround", "imp_open_h", "imp_open_d", "imp_open_a",
            "drift_h", "drift_d", "drift_a", "ha_ratio", "draw_ratio", "fav_implied",
            "home_score", "away_score", "total_goals", "final_result", "result_class",
            "ht_home_score", "ht_away_score", "ht_total", "ht_result", "ht_result_class"]
    out_path = os.path.join(PROJECT_ROOT, OUT)
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=cols)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in cols})

    # 统计
    from collections import Counter
    n_w = sum(1 for x in rows if x["source"] == "william")
    n_i = sum(1 for x in rows if x["source"] == "inter")
    rc = Counter(x["result_class"] for x in rows)
    ht = sum(1 for x in rows if x["ht_result_class"] is not None)
    print(f"总样本: {len(rows)}  (威廉 {n_w} / Inter {n_i})")
    print(f"1X2 标签分布: H={rc.get(0,0)} D={rc.get(1,0)} A={rc.get(2,0)}")
    print(f"有半场标签(威廉): {ht}")
    print(f"日期范围: {min(x['match_date'] for x in rows)} ~ {max(x['match_date'] for x in rows)}")
    print(f"写出: {out_path}")


if __name__ == "__main__":
    main()
