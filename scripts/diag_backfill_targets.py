"""只读诊断 v2: 集合聚合, 避免相关子查询。

遍历:
  1) valid_mids = match_outcomes 中 result 有效且 is_valid=1 的 mid 集合
  2) odds_map[match_key] = (总快照数, 初盘快照数, 滚球快照数)  (一次 GROUP BY 过 odds_snapshots)
  3) 全量 matches(mid 非空), 在 Python 里判定目标
"""
import os, sqlite3, time

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "events.db")

t0 = time.time()
with sqlite3.connect(DB, timeout=60) as c:
    c.row_factory = sqlite3.Row

    print("[1/3] 读取有效赛果 mid ...", flush=True)
    valid_mids = set(r[0] for r in c.execute(
        "SELECT mid FROM match_outcomes WHERE result IN ('home','draw','away') AND is_valid=1 AND mid IS NOT NULL"))
    print(f"    有效赛果 mid 数: {len(valid_mids)}  ({time.time()-t0:.1f}s)", flush=True)

    print("[2/3] 聚合 odds_snapshots (一次 GROUP BY, 可能较慢) ...", flush=True)
    odds_map = {}
    for r in c.execute(
        "SELECT match_key, COUNT(*) AS n,"
        " SUM(CASE WHEN minute_at IS NULL OR minute_at=0 THEN 1 ELSE 0 END) AS n_pre,"
        " SUM(CASE WHEN minute_at>0 THEN 1 ELSE 0 END) AS n_in"
        " FROM odds_snapshots GROUP BY match_key"):
        odds_map[r["match_key"]] = (r["n"], r["n_pre"], r["n_in"])
    print(f"    有赔率的比赛数: {len(odds_map)}  ({time.time()-t0:.1f}s)", flush=True)

    print("[3/3] 遍历 matches 判定目标 ...", flush=True)
    live_only, has_prematch = [], []
    null_mid = 0
    sample_mids = []
    for r in c.execute(
        "SELECT match_key, mid, home, away, league, kickoff, score_home, score_away "
        "FROM matches WHERE mid IS NOT NULL"):
        mk = r["match_key"]
        if mk not in odds_map:
            continue
        if r["mid"] in valid_mids:
            continue
        n, n_pre, n_in = odds_map[mk]
        rec = {"mid": r["mid"], "match_key": mk, "home": r["home"], "away": r["away"],
               "league": r["league"], "kickoff": r["kickoff"],
               "score_home": r["score_home"], "score_away": r["score_away"],
               "n_pre": n_pre, "n_in": n_in}
        if n_pre == 0:
            live_only.append(rec)
        else:
            has_prematch.append(rec)
        if len(sample_mids) < 5:
            sample_mids.append(r["mid"])

print(f"\n[DIAG] 目标总数(有赔率但无有效赛果): {len(live_only)+len(has_prematch)}")
print(f"[DIAG]   live-only(无初盘快照): {len(live_only)}")
print(f"[DIAG]   has-prematch(有初盘快照): {len(has_prematch)}")
print(f"[DIAG]   mid 为空(已排除): {null_mid}")
print(f"[DIAG]   示例 mid: {sample_mids}")

# live-only 中已有有效 result 的数量(上面已排除 valid_mids, 所以这里 live_only 全是无有效 result)
print(f"[DIAG]   live-only 中无有效 result(需回填才能当标签): {len(live_only)}")
print(f"[DIAG]   总耗时: {time.time()-t0:.1f}s")
