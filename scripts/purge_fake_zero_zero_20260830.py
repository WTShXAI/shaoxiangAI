"""清理假 0-0 (2026-08-30, 用户批准)。

问题
----
库里大量"finished"场次比分是 0-0, 但**从未有过任何非零 score_at 快照** ——
即采集器只抓到赔率、没抓到比分, `_sweep_finished` 却按 age 照翻 finished,
比分就地默认 0-0。把"缺失值"写成了**合法但错误**的值。

实测: 全库 finished 11761 场中 0-0 有 1589 场(13.5%), 抽样 1200 场里
**99.2% 从未有过非零比分快照**。近 7 天窗口甚至 62.4%。

后果: 假 0-0 总球=0 → 任何 OU 线的 over 都不中(脏组 over 率仅 6.68% vs
干净组 51.70%); 比分模型"推0-0、库里也假0-0"产生**虚假命中**
(脏组 top1 28.9% vs 干净组 7.6%)。

方案
----
1. 给 matches 加 `score_missing INTEGER DEFAULT 0` 列(可追溯)
2. 命中假 0-0 的场次: score_missing=1, score_home/score_away 置 NULL
   → 下游 `WHERE score_home IS NOT NULL` 自动排除, 且能查出来是"缺失"而非"真0-0"
3. 同步清理 match_outcomes 里由这些假比分生成的脏归档

安全
----
- 默认 dry-run; --apply 才写库
- --apply 前用 VACUUM INTO 做一致性备份(比文件复制快且不阻塞)
- 逐条审计写 data/purge_fake_zero_audit.jsonl

用法
----
    runpy scripts/purge_fake_zero_zero_20260830.py            # dry-run
    runpy scripts/purge_fake_zero_zero_20260830.py --apply    # 落库
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
APPLY = "--apply" in sys.argv
AUDIT = os.path.join(ROOT, "data", "purge_fake_zero_audit.jsonl")


def has_real_score(con, mk) -> bool:
    """采集器是否**真的采集到过比分**。

    ⚠ 2026-08-30 修正: 初版写成 `score_at != '0-0'`, 会把**真实 0-0 的比赛误杀**
      —— 真 0-0 全场每帧 score_at 都是 '0-0', 没有"非零"快照。
      实测初版判定 1612 场里只有 10 场"真 0-0"(0.6%), 远低于真实占比 7~10%。

      正确判据是**有没有过任何比分记录**: 只要采集器抓到过(哪怕一直是 0-0),
      就说明比分是真值; 反之从无比分快照 = 采集器没抓到, 库里的 0-0 是默认值。
    """
    r = con.execute(
        "SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
        "AND score_at!='' LIMIT 1", (mk,)).fetchone()
    return bool(r)


def main():
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row

    # ── 1. 确保 score_missing 列存在 ──
    cols = [r[1] for r in con.execute("PRAGMA table_info(matches)")]
    has_col = "score_missing" in cols
    print(f"score_missing 列: {'已存在' if has_col else '待添加'}")

    # ── 2. 找出假 0-0 (列不存在时不加该条件) ──
    base_sql = (
        "SELECT match_key, home, away, league, kickoff, score_home, score_away, mid "
        "FROM matches WHERE status='finished' AND score_home=0 AND score_away=0")
    if has_col:
        base_sql += " AND (score_missing IS NULL OR score_missing=0)"
    cands = con.execute(base_sql).fetchall()
    print(f"候选( finished + 0-0 且未标记 ): {len(cands)} 场")

    fake = []
    real_zero = []
    for r in cands:
        if has_real_score(con, r["match_key"]):
            real_zero.append(r)          # 真 0-0, 保留
        else:
            fake.append(r)

    print(f"  → 假 0-0 (无比分记录, 待清理): {len(fake)} 场")
    print(f"  → 真 0-0 (有比分记录, 保留)  : {len(real_zero)} 场")
    print()
    print("假 0-0 抽样(前10):")
    for r in fake[:10]:
        print(f"   {str(r['match_key'])[:34]:34s} {str(r['kickoff'])[:16]:16s} "
              f"联赛={str(r['league'] or '')[:14]:14s} mid={r['mid']}")

    # 附带: 这些假 0-0 生成的脏归档
    dirty_outcomes = 0
    if fake:
        q = "SELECT COUNT(*) FROM match_outcomes WHERE mid IN (%s)" % ",".join("?" * len(fake))
        mids = [r["mid"] for r in fake if r["mid"]]
        if mids:
            dirty_outcomes = con.execute(
                "SELECT COUNT(*) FROM match_outcomes WHERE mid IN (%s)" % ",".join("?" * len(mids)),
                mids).fetchone()[0]
    print(f"\n由这些假比分生成的 match_outcomes 脏归档: {dirty_outcomes} 条")

    if not APPLY:
        print("\n[DRY-RUN] 未写库。确认后加 --apply。")
        con.close()
        return

    # ── 3. 备份 (VACUUM INTO, 一致性快照) ──
    bak = os.path.join(ROOT, "data", f"events.db.bak_{time.strftime('%Y%m%d_%H%M%S')}_fakeclear")
    print(f"\n备份 → {bak}")
    con.execute(f"VACUUM INTO '{bak}'")
    print("备份完成")

    # ── 4. 加列 ──
    if not has_col:
        con.execute("ALTER TABLE matches ADD COLUMN score_missing INTEGER DEFAULT 0")
        print("已添加 score_missing 列")

    # ── 5. 标记 + 置 NULL ──
    n = 0
    audit_f = open(AUDIT, "a", encoding="utf-8")
    for r in fake:
        con.execute(
            "UPDATE matches SET score_missing=1, score_home=NULL, score_away=NULL "
            "WHERE match_key=?", (r["match_key"],))
        audit_f.write(json.dumps({
            "ts": int(time.time()), "match_key": r["match_key"], "mid": r["mid"],
            "kickoff": r["kickoff"], "league": r["league"],
            "old_score": "0-0", "action": "score_missing=1, score=NULL",
        }, ensure_ascii=False) + "\n")
        n += 1
    audit_f.close()

    # ── 6. 清理由假比分生成的归档 ──
    n_out = 0
    mids = [r["mid"] for r in fake if r["mid"]]
    if mids:
        cur = con.execute(
            "DELETE FROM match_outcomes WHERE mid IN (%s)" % ",".join("?" * len(mids)), mids)
        n_out = cur.rowcount or 0

    con.commit()
    print(f"\n[APPLY] 已标记并清空假比分: {n} 场")
    print(f"[APPLY] 已删除脏归档: {n_out} 条")
    print(f"[APPLY] 审计: {AUDIT}")
    con.close()


if __name__ == "__main__":
    main()
