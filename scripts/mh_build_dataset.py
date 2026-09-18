"""
Task #2 — 构建多时点 score 模型训练集.
真相源: GQ.db.matches(终场比分=label, 干净). 滚球特征源: events.db.odds_snapshots.
每个对齐 match 取 5 个 checkpoint: kickoff(0)/HT(45)/60/75/85.
  - 特征: 该时点去水 1X2 隐含概率(3) + 当时比分(来自 live feed score_at, 非GQ ht_score防泄漏) + minute
  - label: 终场 1X2(ridx) + 精确比分(sh_final,sa_final)
门控: match_key 仅"主 vs 客" → 跨赛事碰撞, 必须 [kickoff-2h, kickoff+95min] 时间窗.
输出: data/mh_dataset.csv
"""
import sqlite3, csv, math, datetime as dt

GQ_DB = "data/GQ.db"
EV_DB = "data/events.db"
OUT = "data/mh_dataset.csv"
CHECKS = [0, 45, 60, 75, 85]

def parse_kickoff(s):
    if not s: return None
    s = s.strip()
    try: return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception: pass
    try: return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()
    except Exception: pass
    return None

def fair_probs(odds3):
    inv = [1.0 / o if o and o > 0 else 0.0 for o in odds3]
    s = sum(inv)
    if s <= 0: return [0.0, 0.0, 0.0]
    return [x / s for x in inv]

def parse_score(s):
    if not s: return (None, None)
    s = str(s).strip()
    if s in ("", "None", "null") or "-" not in s: return (None, None)
    try:
        a, b = s.split("-", 1); return (int(a), int(b))
    except Exception:
        return (None, None)

cg = sqlite3.connect(GQ_DB, timeout=30); cg.row_factory = sqlite3.Row
ce = sqlite3.connect(EV_DB, timeout=60); ce.row_factory = sqlite3.Row

rows = cg.execute(
    "SELECT match_key, kickoff, score_home, score_away, ht_score_home, ht_score_away "
    "FROM matches WHERE ht_score_home IS NOT NULL "
    "AND score_home IS NOT NULL AND score_away IS NOT NULL"
).fetchall()
print(f"[GQ] candidate matches: {len(rows)}")

def odds_at(mk, kt, target_epoch, is_pre):
    """返回 (imp_home,imp_draw,imp_away, cur_sh,cur_sa, minute) 或 None.
    minute 用 captured_at-kt 推算(可靠), 不用 feed 的 minute_at(常滞后)."""
    if is_pre:
        ca = ce.execute(
            "SELECT captured_at FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at<=? ORDER BY captured_at DESC LIMIT 1", (mk, kt)
        ).fetchone()
    else:
        lo, hi = target_epoch - 300, target_epoch + 300
        ca = ce.execute(
            "SELECT captured_at FROM odds_snapshots WHERE match_key=? AND market='1X2' "
            "AND captured_at BETWEEN ? AND ? ORDER BY ABS(captured_at-?) ASC LIMIT 1",
            (mk, lo, hi, target_epoch)
        ).fetchone()
    if not ca: return None
    ca = ca["captured_at"]
    snap = ce.execute(
        "SELECT selection, odds, score_at FROM odds_snapshots "
        "WHERE match_key=? AND market='1X2' AND captured_at=?", (mk, ca)
    ).fetchall()
    o = {}; sa = None
    for x in snap:
        o[x["selection"]] = x["odds"]; sa = x["score_at"]
    if not all(k in o for k in ("home", "draw", "away")): return None
    imp = fair_probs([o["home"], o["draw"], o["away"]])
    cur = parse_score(sa)
    if is_pre:
        csh, csa, mn = 0, 0, 0
    else:
        if cur[0] is None or cur[1] is None: return None
        csh, csa = cur[0], cur[1]
        mn = max(0, round((ca - kt) / 60.0))
    return (imp[0], imp[1], imp[2], csh, csa, mn)

out = open(OUT, "w", newline="", encoding="utf-8")
w = csv.writer(out)
w.writerow(["match_key", "cp", "imp_home", "imp_draw", "imp_away",
            "cur_sh", "cur_sa", "minute", "sh_final", "sa_final", "ridx", "kickoff_epoch"])

n_match = n_sample = 0
for r in rows:
    mk = r["match_key"]; kt = parse_kickoff(r["kickoff"])
    if kt is None: continue
    sh_f, sa_f = int(r["score_home"]), int(r["score_away"])
    ridx = 0 if sh_f > sa_f else (2 if sa_f > sh_f else 1)
    matched_any = False
    for cp in CHECKS:
        is_pre = (cp == 0)
        target = kt + cp * 60
        od = odds_at(mk, kt, target, is_pre)
        if od is None: continue
        ih, idr, ia, csh, csa, mn = od
        # 过滤 feed 错误: 当前比分不可能超过终场(7/4760, 丢弃)
        if not is_pre and (csh > sh_f or csa > sa_f): continue
        w.writerow([mk, cp, f"{ih:.6f}", f"{idr:.6f}", f"{ia:.6f}",
                    csh, csa, mn, sh_f, sa_f, ridx, f"{kt:.1f}"])
        n_sample += 1; matched_any = True
    if matched_any: n_match += 1

out.close()
print(f"[DONE] aligned matches with >=1 checkpoint: {n_match}")
print(f"[DONE] total samples (match x checkpoint): {n_sample}")
print(f"[OUT] {OUT}")
