# -*- coding: utf-8 -*-
"""WS2 影子库验证: 在 GQ_shadow.db 副本上复刻 _sweep_finished 的状态清理逻辑,
统计僵尸清理前后 status 分布。只读/只写影子副本, 绝不碰生产 events.db。
用法: python scripts/ws2_shadow_validate.py [shadow_path]
"""
import sqlite3, sys, time
from datetime import datetime, timezone, timedelta

SHADOW = sys.argv[1] if len(sys.argv) > 1 else "data/GQ_shadow.db"

def dist(cur):
    rows = cur.execute("SELECT status, COUNT(*) FROM matches GROUP BY status").fetchall()
    return {r[0]: r[1] for r in rows}

def parse_kickoff(s):
    try:
        kt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        return kt.replace(tzinfo=timezone(timedelta(hours=8)))
    except Exception:
        return None

def main():
    conn = sqlite3.connect(SHADOW, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    now = time.time()
    print(f"[shadow] {SHADOW}  size={__import__('os').path.getsize(SHADOW)/1e9:.2f}GB")

    before = dist(cur)
    print("[before] status 分布:", before)

    live_rows = cur.execute(
        "SELECT match_key, kickoff, score_home, score_away, minute FROM matches "
        "WHERE status='live' AND kickoff IS NOT NULL AND kickoff != ''").fetchall()
    total_live = len(live_rows)
    zombie = real_live = future = 0
    for r in live_rows:
        kt = parse_kickoff(r["kickoff"])
        if kt is None:
            real_live += 1; continue
        age = now - kt.timestamp()
        sc = (r["score_home"] or 0) + (r["score_away"] or 0)
        mn = r["minute"] or 0
        if age < 0:
            future += 1
        elif age >= 2.5 * 3600 or sc > 0 or mn >= 90:
            zombie += 1
        else:
            real_live += 1
    print(f"[scan] live 总计={total_live}  真进行中≈{real_live}  僵尸(可清理)={zombie}  未来误标={future}")

    # 在影子副本上执行清理 (与生产 _sweep_finished 同逻辑, 仅写 shadow)
    upd_fin = upd_sched = 0
    for r in live_rows:
        kt = parse_kickoff(r["kickoff"])
        if kt is None:
            continue
        age = now - kt.timestamp()
        sc = (r["score_home"] or 0) + (r["score_away"] or 0)
        mn = r["minute"] or 0
        if age < 0:
            cur.execute("UPDATE matches SET status='scheduled', minute=0 WHERE match_key=?",
                        (r["match_key"],)); upd_sched += 1
        elif age >= 2.5 * 3600 or sc > 0 or mn >= 90:
            cur.execute("UPDATE matches SET status='finished', minute=90 WHERE match_key=?",
                        (r["match_key"],)); upd_fin += 1
    conn.commit()
    after = dist(cur)
    print(f"[apply] shadow 内修正: →finished={upd_fin} →scheduled={upd_sched}")
    print("[after ] status 分布:", after)
    print(f"[结论] 清理后 live: {after.get('live',0)} (清理前 {total_live}); "
          f"僵尸清零={zombie==upd_fin}")
    conn.close()

if __name__ == "__main__":
    main()
