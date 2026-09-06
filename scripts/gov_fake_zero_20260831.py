"""Fix-1 假 0-0 治理 (2026-08-31, IR-04 改良口径).

判定: 终场 0:0 的场次必须有开赛后 (captured_at > kickoff-300s) 的 score_at 非空快照
      (=采集器跟踪过比赛), 否则判假 0-0 → 打 score_missing=1 标记 (不删行/不改比分, 可逆).

用法:
  python scripts/gov_fake_zero_20260831.py --dry-run   # 只读统计, 不改库
  python scripts/gov_fake_zero_20260831.py --apply     # 备份 matches 表 + 打标 + 审计 jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "events.db")
BACKUP_TABLE = "matches_bak_20260831_2305"
AUDIT = os.path.join(ROOT, "data", "fake_zero_gov_audit_20260831.jsonl")

TZ8 = timezone.utc  # parse_kickoff 统一按 UTC 解析(GQ kickoff 多为 UTC 格式)


def parse_kickoff(k: str) -> float | None:
    if not k:
        return None
    k = k.strip()
    for fmt, tz in [("%Y-%m-%d %H:%M", None), ("%Y-%m-%dT%H:%M:%SZ", timezone.utc),
                    ("%Y-%m-%dT%H:%M:%S", None)]:
        try:
            dt = datetime.strptime(k, fmt)
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
            return dt.timestamp()
        except ValueError:
            continue
    return None


def has_inplay_score(c: sqlite3.Connection, match_key: str, ko_ts: float) -> bool:
    return c.execute(
        "SELECT 1 FROM odds_snapshots WHERE match_key=? "
        "AND score_at IS NOT NULL AND score_at!='' AND captured_at > ? LIMIT 1",
        (match_key, ko_ts - 300),
    ).fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只读统计不改库")
    ap.add_argument("--apply", action="store_true", help="备份+打标+审计 (默认)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # 待治理: finished + 0:0 + 未打标
    rows = cur.execute("""
        SELECT match_key, kickoff FROM matches
        WHERE status='finished' AND score_home=0 AND score_away=0
          AND (score_missing IS NULL OR score_missing != 1)
    """).fetchall()
    print(f"[Fix-1] finished 0:0 未打标候选: {len(rows)} 场")

    fake, real, no_ko = [], [], []
    for mk, ko in rows:
        ko_ts = parse_kickoff(ko)
        if ko_ts is None:
            no_ko.append({"match_key": mk, "kickoff": ko, "judge": "NO_KICKOFF"})
            continue
        if has_inplay_score(conn, mk, ko_ts):
            real.append({"match_key": mk, "kickoff": ko, "judge": "REAL_00"})
        else:
            fake.append({"match_key": mk, "kickoff": ko, "judge": "FAKE_00"})

    print(f"  真 0-0 (有滚球佐证): {len(real)}")
    print(f"  假 0-0 (无滚球佐证): {len(fake)}")
    print(f"  无 kickoff 无法判定: {len(no_ko)}")

    if args.dry_run:
        # 展示前 10 条假 0-0 供抽查
        for r in fake[:10]:
            print("    FAKE:", r["match_key"], r["kickoff"])
        print("[dry-run] 未写库。")
        conn.close()
        return 0

    # ── 执行: 备份 + 打标 + 审计 ──
    # 1) 表级备份 (matches 仅 1.3 万行, 秒级)
    has_bak = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (BACKUP_TABLE,)
    ).fetchone()
    if has_bak:
        print(f"[Fix-1] 备份表 {BACKUP_TABLE} 已存在, 跳过备份")
    else:
        cur.execute(f"CREATE TABLE {BACKUP_TABLE} AS SELECT * FROM matches")
        print(f"[Fix-1] 已备份 matches → {BACKUP_TABLE} ({cur.execute(f'SELECT COUNT(*) FROM {BACKUP_TABLE}').fetchone()[0]} 行)")

    # 2) 打标假 0-0
    n_tag = 0
    with open(AUDIT, "w", encoding="utf-8") as f:
        for r in fake:
            cur.execute(
                "UPDATE matches SET score_missing=1 WHERE match_key=? AND status='finished'",
                (r["match_key"],),
            )
            n_tag += cur.rowcount
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for r in no_ko:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    conn.commit()
    print(f"[Fix-1] 已打标 {n_tag} 场假 0-0 → score_missing=1")
    print(f"[Fix-1] 审计: {AUDIT}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
