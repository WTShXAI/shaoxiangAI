"""
backfill_match_outcomes_results.py — 一次性回填赛果 SSoT
------------------------------------------------------------------
把 matches 表里「已完赛 + 有真实比分 + 数字 mid 已存在 + 但 match_outcomes
里没有这行」的比赛, 補进 match_outcomes, 使赛果 SSoT 与 matches 一致.

关键事实 (2026-08-03 复核):
  - match_outcomes.mid 是 GQ 数字 ID (如 5323651)
  - matches 同时有 match_key(队名串) 与 mid(数字 ID), 两表经 m.mid=o.mid 关联
  - 之前误用 match_key NOT IN (SELECT mid) 比较, 产出假的 1803/1827, 实为 apples-vs-oranges
  - 真实缺口 (finished + score 非空 + mid 非空 + 不在 match_outcomes) = 266 场

回填字段:
  mid / home / away / league / kickoff / score_home / score_away / result
  op_cs='[]', op_1x2_*/op_ah_*/op_ou_* 留 NULL (这些比赛本无初盘赔率)
  captured_at=matches.first_seen (初盘采集时间缺失, 用首次见时间代理)
  archived_at=now, is_valid=1, source='gq'

安全:
  - 仅插入 mid NOT IN (SELECT mid FROM match_outcomes) 的行
  - 整体事务, 失败回滚
  - 回填的 mid 清单写入 data/backfilled_match_outcomes_<date>.json 留痕
"""
import sqlite3
import time
import json
import os
import sys

DB = "data/events.db"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "..", DB) if not os.path.isabs(DB) else DB
DB_PATH = os.path.normpath(DB_PATH)


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    # 真实缺口
    q = """
        SELECT m.mid, m.home, m.away, m.league, m.kickoff,
               m.score_home, m.score_away, m.first_seen
        FROM matches m
        WHERE m.status='finished'
          AND m.score_home IS NOT NULL AND m.score_away IS NOT NULL
          AND m.mid IS NOT NULL
          AND m.mid NOT IN (SELECT mid FROM match_outcomes)
    """
    rows = cur.execute(q).fetchall()
    print(f"[dry] 待回填场数: {len(rows)}")

    if not rows:
        print("[info] 无需回填")
        return

    # 防重二次确认 (理论上 UNIQUE 已挡, 这里再保险)
    existing = set(r[0] for r in cur.execute("SELECT mid FROM match_outcomes"))
    rows = [r for r in rows if r[0] not in existing]
    print(f"[dry] 去重后实际插入: {len(rows)}")

    now = time.time()
    inserted_ids = []
    try:
        for mid, home, away, league, kickoff, sh, sa, first_seen in rows:
            if sh > sa:
                result = "home"
            elif sh < sa:
                result = "away"
            else:
                result = "draw"
            cap = first_seen if first_seen else now
            cur.execute(
                """INSERT INTO match_outcomes
                   (mid, home, away, league, kickoff,
                    score_home, score_away, result,
                    op_cs, captured_at, archived_at, is_valid, source)
                   VALUES (?,?,?,?,?, ?,?,?, '[]', ?,?, 1, 'gq')""",
                (mid, home, away, league or "", kickoff,
                 int(sh), int(sa), result,
                 float(cap), float(now)),
            )
            inserted_ids.append(mid)
        con.commit()
        print(f"[ok] 已提交 {len(inserted_ids)} 行")
    except Exception as e:
        con.rollback()
        print(f"[ERR] 回滚: {e}")
        raise

    # 验证: 回填后这些 mid 是否都在 match_outcomes
    check = cur.execute(
        "SELECT COUNT(*) FROM match_outcomes WHERE mid IN ({})".format(
            ",".join("?" * len(inserted_ids))
        ),
        inserted_ids,
    ).fetchone()[0]
    print(f"[verify] 回填后 match_outcomes 含这些 mid: {check}/{len(inserted_ids)}")

    # 留痕
    out = os.path.join(SCRIPT_DIR, "..", "data",
                       f"backfilled_match_outcomes_{time.strftime('%Y-%m-%d')}.json")
    out = os.path.normpath(out)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "count": len(inserted_ids),
            "backfilled_at": now,
            "note": "finished+scored matches missing from match_outcomes, keyed by numeric mid",
            "mids": inserted_ids,
        }, f, ensure_ascii=False, indent=2)
    print(f"[trace] 清单已写入 {out}")


if __name__ == "__main__":
    main()
