"""
Task #1 — 多时点 score 模型: 干净数据底座覆盖测量.
GQ.db.matches = 真相源(含 ht_score + 终场比分), kickoff 为本地时间字符串(北京 UTC+8).
events.db.odds_snapshots = 滚球 1X2 时序, captured_at 为 Unix epoch(UTC).
match_key 仅 "主 vs 客" → 跨赛事碰撞, 必须按时间窗 [kickoff-2h, kickoff+95min] 门控.
"""
import sqlite3, datetime as dt

# 复用 p0_clv_clean.py:36-45 权威解析(处理 ISO+tz 与 naive 本地两种格式)
def kickoff_epoch(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()

cg = sqlite3.connect("data/GQ.db", timeout=30); cg.row_factory = sqlite3.Row
ce = sqlite3.connect("data/events.db", timeout=60); ce.row_factory = sqlite3.Row

rows = cg.execute(
    "SELECT match_key, kickoff, score_home, score_away, ht_score_home, ht_score_away "
    "FROM matches WHERE ht_score_home IS NOT NULL"
).fetchall()
print(f"[GQ] HT-score matches total: {len(rows)}")

aligned_pre = aligned_live = aligned_both = 0   # both pre(<=kickoff) + live(>kickoff) 1X2
live_into_2h = 0   # 有滚球 odds 延伸到 2H (kickoff+45min 之后仍有点)
checkpoints_present = {45: 0, 60: 0, 75: 0, 85: 0}  # 各时点至少有1个 live 1X2 快照的 match 数

for r in rows:
    mk = r["match_key"]; k = kickoff_epoch(r["kickoff"])
    lo, hi = k - 7200, k + 5700   # kickoff-2h .. kickoff+95min
    agg = ce.execute(
        "SELECT "
        " SUM(CASE WHEN captured_at <= ? THEN 1 ELSE 0 END) AS pre_n,"
        " SUM(CASE WHEN captured_at >  ? THEN 1 ELSE 0 END) AS live_n,"
        " SUM(CASE WHEN captured_at >  ? + 2700 THEN 1 ELSE 0 END) AS live_2h_n"  # >kickoff+45min
        " FROM odds_snapshots WHERE match_key=? AND market='1X2' AND captured_at BETWEEN ? AND ?",
        (k, k, k, mk, lo, hi)
    ).fetchone()
    pre_n = agg["pre_n"] or 0
    live_n = agg["live_n"] or 0
    live_2h = agg["live_2h_n"] or 0
    if pre_n > 0: aligned_pre += 1
    if live_n > 0: aligned_live += 1
    if pre_n > 0 and live_n > 0: aligned_both += 1
    if live_2h > 0: live_into_2h += 1
    # 各时点采样: 取该时点±5min窗内是否有 live 1X2 快照
    for cp in checkpoints_present:
        cp_lo, cp_hi = k + cp*60 - 300, k + cp*60 + 300
        n = ce.execute(
            "SELECT 1 FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at BETWEEN ? AND ? LIMIT 1",
            (mk, max(lo, cp_lo), min(hi, cp_hi))
        ).fetchone()
        if n: checkpoints_present[cp] += 1

print(f"[ALIGN] has pre 1X2 : {aligned_pre}")
print(f"[ALIGN] has live 1X2: {aligned_live}")
print(f"[ALIGN] has BOTH    : {aligned_both}  <-- 多时点训练宇宙")
print(f"[ALIGN] live into 2H: {live_into_2h}")
for cp in sorted(checkpoints_present):
    print(f"[CP@{cp:>2}'] 窗口内有点: {checkpoints_present[cp]}")

# 碰撞抽样对照: 取一个 match_key, 看其全部 odds 时间跨度(确认门控必要性)
mk0 = rows[0]["match_key"]
span = ce.execute(
    "SELECT MIN(captured_at) mn, MAX(captured_at) mx, COUNT(*) n "
    "FROM odds_snapshots WHERE match_key=? AND market='1X2'", (mk0,)
).fetchone()
if span and span["n"]:
    print(f"[COLLISION-CHECK] {mk0!r}: odds n={span['n']} "
          f"span=[{dt.datetime.fromtimestamp(span['mn']):%Y-%m-%d %H:%M} .. "
          f"{dt.datetime.fromtimestamp(span['mx']):%Y-%m-%d %H:%M}] "
          f"vs kickoff={rows[0]['kickoff']}")
