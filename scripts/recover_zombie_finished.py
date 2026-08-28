# -*- coding: utf-8 -*-
"""
recover_zombie_finished.py  (2026-08-27)
=======================================
僵尸场回收: status='live' 且 last_seen 超 STALE_HOURS 无更新(几乎必然已终场,
但 watcher 未翻转/未落 match_outcomes) 的比赛, 用 matches 末次全场比分作为
"最佳可得真相" 强制落标, 解锁原本永久丢失的训练标签。

规则(诚实边界, 宁缺勿错):
  - 仅处理: status='live' AND last_seen < now-STALE_HOURS AND 全场比分非空
  - 已有 match_outcomes(result 非空) 的: 仅翻 status='finished', 不重插标签
  - 缺 match_outcomes 的: 翻 finished + 插一行, source='forced_status_recovery'
  - ht_score 留 NULL(matches.ht 已被全场覆盖污染, 不传播)
  - 全场比分缺失的: 跳过(不伪造)
  - 全部带 in-play 快照(实测 593/593) -> 落标即被 retrain 纳入

幂等: INSERT 用 NOT EXISTS(home,away,result NOT NULL) 守卫; UPDATE 仅改 status='live'。
安全: 落盘前 cp 备份 events.db; 支持 DRY_RUN=1 只读预览。

用法:
  DRY_RUN=1 python scripts/recover_zombie_finished.py   # 只读预览
  python scripts/recover_zombie_finished.py             # 执行
"""
import os, sys, shutil, sqlite3, hashlib, datetime as dt

ROOT = r"D:\Architecture"
GQ = os.path.join(ROOT, "data", "events.db")
STALE_HOURS = 6


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def epoch():
    return dt.datetime.now(dt.timezone.utc).timestamp()


def conn():
    c = sqlite3.connect(GQ, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    return c


def synth_mid(match_key):
    return "rec_" + hashlib.md5(match_key.encode("utf-8")).hexdigest()[:12]


def main():
    dry = os.environ.get("DRY_RUN") == "1"
    cut = (now_utc() - dt.timedelta(hours=STALE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[recover] cutoff(utc-{STALE_HOURS}h): {cut}  DRY_RUN={dry}")

    c = conn()

    # 候选: live + 过期 + 有比分 + 无有效 match_outcomes
    cand = c.execute("""
        SELECT m.match_key, m.mid, m.home, m.away, m.league, m.kickoff,
               m.score_home, m.score_away
        FROM matches m
        WHERE m.status='live' AND m.last_seen < ?
          AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM match_outcomes o
                          WHERE o.home=m.home AND o.away=m.away AND o.result IS NOT NULL)
    """, (cut,)).fetchall()

    # 仅翻 status: 有比分 + 已有 match_outcomes
    flip_only = c.execute("""
        SELECT COUNT(*) FROM matches m
        WHERE m.status='live' AND m.last_seen < ?
          AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND EXISTS (SELECT 1 FROM match_outcomes o
                      WHERE o.home=m.home AND o.away=m.away AND o.result IS NOT NULL)
    """, (cut,)).fetchone()[0]

    # 无比分僵尸: 跳过
    no_score = c.execute("""
        SELECT COUNT(*) FROM matches
        WHERE status='live' AND last_seen < ?
          AND (score_home IS NULL OR score_away IS NULL)
    """, (cut,)).fetchone()[0]

    null_mid = sum(1 for r in cand if r[1] is None)
    print(f"[recover] 候选(插标签+翻status): {len(cand)}  (其中 mid 缺失将由 hash 合成: {null_mid})")
    print(f"[recover] 仅翻status(已有标签): {flip_only}")
    print(f"[recover] 无比分跳过: {no_score}")

    if dry:
        print("[recover] DRY_RUN: 未写盘, 退出")
        c.close()
        return

    # ---- 备份 ----
    ts = now_utc().strftime("%Y%m%d_%H%M")
    bak = GQ + f".bak_zombie_{ts}"
    shutil.copy2(GQ, bak)
    print(f"[recover] 已备份: {bak}")

    ins_cols = ["mid", "home", "away", "league", "kickoff",
                "score_home", "score_away", "result", "source",
                "is_valid", "captured_at", "archived_at", "is_override", "is_virtual"]
    sch = [x[1] for x in c.execute("PRAGMA table_info(match_outcomes)")]
    ins_cols = [x for x in ins_cols if x in sch]
    q = "INSERT INTO match_outcomes (%s) VALUES (%s)" % (
        ",".join(ins_cols), ",".join("?" * len(ins_cols)))

    inserted = 0
    skipped_integrity = 0
    t = epoch()
    for mk, mid, home, away, league, kickoff, sh, sa in cand:
        res = "H" if sh > sa else ("A" if sa > sh else "D")
        use_mid = mid if mid is not None else synth_mid(mk)
        vals = []
        for col in ins_cols:
            if col == "mid": vals.append(use_mid)
            elif col == "home": vals.append(home)
            elif col == "away": vals.append(away)
            elif col == "league": vals.append(league)
            elif col == "kickoff": vals.append(kickoff)
            elif col == "score_home": vals.append(sh)
            elif col == "score_away": vals.append(sa)
            elif col == "result": vals.append(res)
            elif col == "source": vals.append("forced_status_recovery")
            elif col == "is_valid": vals.append(1)
            elif col == "captured_at": vals.append(t)
            elif col == "archived_at": vals.append(t)
            elif col == "is_override": vals.append(0)
            elif col == "is_virtual": vals.append(0)
        try:
            c.execute(q, vals)
            inserted += 1
        except sqlite3.IntegrityError as e:
            skipped_integrity += 1
            print(f"  [跳过] {mk}: IntegrityError {e}")

    # 统一翻 status='finished' (覆盖 候选 + 仅翻status 两组)
    c.execute("""
        UPDATE matches SET status='finished'
        WHERE status='live' AND last_seen < ?
          AND score_home IS NOT NULL AND score_away IS NOT NULL
    """, (cut,))
    flipped = c.execute("SELECT changes()").fetchone()[0]

    c.commit()
    print(f"[recover] 已插 match_outcomes: {inserted} (跳过 Integrity: {skipped_integrity})")
    print(f"[recover] 已翻 status='finished': {flipped}")
    c.close()


if __name__ == "__main__":
    main()
