# -*- coding: utf-8 -*-
"""WS3 修复: 还原 match_analysis_cache.mid 损坏的 "home vs away" 队名串为正确数字比赛 id.
前置: 先备份 (见 main). 还原依据 matches(home,away) -> 多场时优先 finished+有比分,
否则按 kickoff 与 cache.captured_at 最接近者. 同目标 mid 多行去重保留 captured_at 最大者.
幂等: 仅处理非纯数字 mid; 数字 mid 行不动.
"""
import sqlite3, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collections import defaultdict
import gq.db as db

BAK = "match_analysis_cache_bak_20260808"

def load_matches(conn):
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT mid, home, away, kickoff, status, score_home FROM matches").fetchall()
    # (home,away) -> list of (mid, kickoff_ts, has_score)
    d = defaultdict(list)
    for r in rows:
        h, a = (r["home"] or "").strip(), (r["away"] or "").strip()
        if not h or not a:
            continue
        kt = None
        try:
            kt = time.mktime(time.strptime(r["kickoff"][:16], "%Y-%m-%d %H:%M"))
        except Exception:
            kt = None
        has_score = 1 if (r["status"] == "finished" and r["score_home"] is not None) else 0
        d[(h, a)].append((str(r["mid"]), kt, has_score))
    return d

def resolve(mkey, cap, mdict):
    cands = mdict.get(mkey)
    if not cands:
        return None
    best, best_s = None, -1e18
    for mid, kt, has_score in cands:
        s = has_score * 2.0
        if kt is not None and cap:
            s -= abs(kt - cap) / 1e12   # 极轻权重: kickoff 越贴近 captured_at 越好
        if s > best_s:
            best_s, best = s, mid
    return best

def main():
    conn = sqlite3.connect(db.DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1) 备份
    cur.execute(f"CREATE TABLE IF NOT EXISTS {BAK} AS SELECT * FROM match_analysis_cache")
    conn.commit()
    bak_n = cur.execute(f"SELECT COUNT(*) FROM {BAK}").fetchone()[0]
    print(f"[backup] {BAK}: {bak_n} rows")

    rows = cur.execute("SELECT analysis_id, mid, captured_at FROM match_analysis_cache").fetchall()
    numeric, corrupt = [], []
    for r in rows:
        m = r["mid"]
        if m is not None and str(m).isdigit():
            numeric.append(r)
        else:
            corrupt.append(r)
    print(f"[scan] total={len(rows)} numeric={len(numeric)} corrupt={len(corrupt)}")

    print("[load] matches ...")
    mdict = load_matches(conn)
    print(f"[load] distinct (home,away) keys: {len(mdict)}")

    plan = []  # (analysis_id, target_mid_or_None)
    unresolved = 0
    for r in corrupt:
        h_a = r["mid"]
        if not isinstance(h_a, str) or " vs " not in h_a:
            plan.append((r["analysis_id"], None)); unresolved += 1; continue
        h, a = h_a.split(" vs ", 1)
        h, a = h.strip(), a.strip()
        tgt = resolve((h, a), r["captured_at"] or 0, mdict)
        plan.append((r["analysis_id"], tgt))
        if tgt is None:
            unresolved += 1

    # 分组: 目标 mid -> [analysis_id]
    target_map = defaultdict(list)
    none_ids = []
    for aid, tmid in plan:
        if tmid is None:
            none_ids.append(aid)
        else:
            target_map[tmid].append(aid)

    existing_numeric = {str(r["mid"]) for r in numeric}
    cap_of = lambda aid: cur.execute(
        "SELECT captured_at FROM match_analysis_cache WHERE analysis_id=?", (aid,)).fetchone()[0] or 0

    updates = deletes = collisions = 0
    for tmid, aids in target_map.items():
        if str(tmid) in existing_numeric:
            # 与既有的数字 mid 行冲突 -> 合并保留 captured_at 最大者
            num_row = next((r for r in numeric if str(r["mid"]) == str(tmid)), None)
            all_ids = aids + ([num_row["analysis_id"]] if num_row else [])
            keeper = max(all_ids, key=cap_of)
            for aid in all_ids:
                if aid != keeper:
                    cur.execute("DELETE FROM match_analysis_cache WHERE analysis_id=?", (aid,)); deletes += 1
            if keeper in aids:
                cur.execute("UPDATE match_analysis_cache SET mid=? WHERE analysis_id=?", (str(tmid), keeper)); updates += 1
            collisions += 1
        else:
            if len(aids) == 1:
                cur.execute("UPDATE match_analysis_cache SET mid=? WHERE analysis_id=?", (str(tmid), aids[0])); updates += 1
            else:
                keeper = max(aids, key=cap_of)
                for aid in aids:
                    if aid != keeper:
                        cur.execute("DELETE FROM match_analysis_cache WHERE analysis_id=?", (aid,)); deletes += 1
                cur.execute("UPDATE match_analysis_cache SET mid=? WHERE analysis_id=?", (str(tmid), keeper)); updates += 1

    conn.commit()
    dups = cur.execute("SELECT COUNT(*) FROM (SELECT mid FROM match_analysis_cache GROUP BY mid HAVING COUNT(*)>1)").fetchone()[0]
    # 重算状态
    n_num = cur.execute("SELECT COUNT(*) FROM match_analysis_cache WHERE mid GLOB '*[0-9]*'").fetchone()[0]
    n_cor = cur.execute("SELECT COUNT(*) FROM match_analysis_cache WHERE mid IS NOT NULL AND mid NOT GLOB '*[0-9]*'").fetchone()[0]
    print(f"[result] updates={updates} deletes={deletes} unresolved_left={unresolved} collisions={collisions}")
    print(f"[result] dup_mid_groups={dups} | 现数字mid行={n_num} 仍损坏mid行={n_cor}")
    conn.close()
    print("[done] 若需回滚: DELETE FROM match_analysis_cache; INSERT INTO match_analysis_cache SELECT * FROM", BAK)

if __name__ == "__main__":
    main()
