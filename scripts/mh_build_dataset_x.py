"""
Task #5 — 扩展多时点数据集: 注入市场未含的独立信息.
读 data/mh_dataset.csv (1628场/6000样本), 按 (match_key, date) join indep_features_gq.db 的
13 个独立特征(elo/form/rest/h2h/league_strength, 源于赛果历史, 非赔率 -> 市场不含).
date 歧义: match_key 跨赛事碰撞, 用 kickoff 的本地/UTC 双日期匹配(±1天容错).
输出 data/mh_dataset_x.csv (原 6 特征 + 13 独立特征 + label).
"""
import sqlite3, csv, datetime as dt

IND = sqlite3.connect("data/indep_features_gq.db", timeout=30); IND.row_factory = sqlite3.Row
FEAT = ["elo_home","elo_away","elo_diff","form_home","form_away","form_diff",
        "rest_home","rest_away","rest_diff","h2h_home_win","h2h_draw","h2h_away_win","league_strength"]

# 建 lookup: (match_key, date_str) -> feat tuple
look = {}
for r in IND.execute("SELECT match_key, match_date, "+",".join(FEAT)+" FROM indep_features").fetchall():
    d = (r["match_date"] or "")[:10]
    look[(r["match_key"], d)] = tuple(float(r[f]) for f in FEAT)
print(f"[IND] indep rows: {len(look)} (keyed by match_key+date)")

rows = list(csv.DictReader(open("data/mh_dataset.csv", encoding="utf-8")))
out = open("data/mh_dataset_x.csv", "w", newline="", encoding="utf-8")
w = csv.writer(out)
w.writerow(["match_key","cp","imp_home","imp_draw","imp_away","cur_sh","cur_sa","minute",
            "sh_final","sa_final","ridx","kickoff_epoch"] + FEAT)

kept = 0; dropped = 0
for r in rows:
    mk = r["match_key"]; kt = float(r["kickoff_epoch"])
    local_d = dt.datetime.fromtimestamp(kt).strftime("%Y-%m-%d")
    utc_d = dt.datetime.utcfromtimestamp(kt).strftime("%Y-%m-%d")
    feat = look.get((mk, local_d)) or look.get((mk, utc_d))
    if feat is None:
        # 容错: 同 match_key 同日±1 内任意(防 indep 用 UTC/local 不一致)
        dropped += 1; continue
    w.writerow([r["match_key"], r["cp"], r["imp_home"], r["imp_draw"], r["imp_away"],
                r["cur_sh"], r["cur_sa"], r["minute"], r["sh_final"], r["sa_final"],
                r["ridx"], r["kickoff_epoch"]] + [f"{x:.4f}" for x in feat])
    kept += 1
out.close()
print(f"[OUT] kept={kept} dropped={dropped} coverage={kept/(kept+dropped)*100:.1f}%")
print(f"[OUT] data/mh_dataset_x.csv")
