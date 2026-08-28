# -*- coding: utf-8 -*-
"""回溯补档 match_outcomes。

背景
----
gq/db.py::get_opening_odds 用 market[3:] 切 line key, 半场盘口
market='AH_1H_2.50' / 'OU_1H_2.50' 会产出 key='1H_2.50' 混进全场字典。
record_match_outcome 旧代码直接 float(key) 排序 -> ValueError ->
整个归档函数异常退出 -> **该场比赛彻底没写进 match_outcomes**。
库内 420 场含半场盘口, 已确认 326 场因此丢档。

另有一批场次是「采集器错过归档窗口」(比赛结束时采集器不在线 / 卡在 45',
比分是后来靠 500网联网核对补上的), 同样没有 outcomes 记录。

本脚本把这两类一并补回, 遵守「有据可查」铁律。

安全设计
--------
- 默认 dry-run, 只统计不写库; --apply 才真正写入。
- --apply 前自动备份 events.db -> events.db.bak_YYYYmmdd_HHMMSS。
- 复用 gq.db.record_match_outcome, 幂等: 已存在的 mid 绝不重复 INSERT。
- 沿用原有业务规则: 友谊赛跳过、无比分跳过。
- 逐条审计追加 data/backfill_outcomes_audit.jsonl, 可回退。

用法
----
    python scripts/backfill_outcomes.py            # dry-run
    python scripts/backfill_outcomes.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "data", "events.db")
AUDIT = os.path.join(ROOT, "data", "backfill_outcomes_audit.jsonl")

# 虚拟电子足球(8 分钟赛制), 非真实比赛。
# 与友谊赛同理: 不进 match_outcomes 建模/复盘库, 但完整保留在 matches(有据可查)。
VIRTUAL_KEYWORDS = ("瓦尔哈拉杯", "瓦尔基里杯", "(8分钟)", "（8分钟）")


def is_virtual(league: str) -> bool:
    lg = league or ""
    return any(k in lg for k in VIRTUAL_KEYWORDS)


def fetch_candidates(conn: sqlite3.Connection) -> list[dict]:
    """已完场(finished) + 有终场比分 + 非友谊赛 + match_outcomes 无记录 的场次。

    注意: 必须限定 status='finished', 否则 live(进行中)比赛带实时比分会被误判为
    应归档缺档并错误锁死终场. kickoff=0 脏时间场也纳入(归档窗口算不出导致漏建).
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT m.mid, m.league, m.home, m.away, m.kickoff,
               m.score_home, m.score_away, m.ht_score_home, m.ht_score_away
        FROM matches m
        WHERE m.score_home IS NOT NULL
          AND m.score_away IS NOT NULL
          AND m.status = 'finished'
          AND m.league NOT LIKE '%友谊%'
          AND NOT EXISTS (SELECT 1 FROM match_outcomes o WHERE o.mid = m.mid)
        ORDER BY m.kickoff
        """
    ).fetchall()
    return [dict(r) for r in rows]


def has_halftime_line(conn: sqlite3.Connection, home: str, away: str) -> bool:
    """该场是否存在半场盘口(即旧代码会崩溃的场次)。"""
    key = f"{home} vs {away}"
    r = conn.execute(
        "SELECT 1 FROM odds_snapshots WHERE match_key=? "
        "AND (market LIKE 'AH_1H_%' OR market LIKE 'OU_1H_%') LIMIT 1",
        (key,),
    ).fetchone()
    return r is not None


def has_any_odds(conn: sqlite3.Connection, home: str, away: str) -> bool:
    key = f"{home} vs {away}"
    r = conn.execute(
        "SELECT 1 FROM odds_snapshots WHERE match_key=? LIMIT 1", (key,)
    ).fetchone()
    return r is not None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库(默认 dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 场(调试用)")
    ap.add_argument("--include-virtual", action="store_true",
                    help="把虚拟电子足球(8分钟赛制)也写进 match_outcomes(默认排除)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    cands = fetch_candidates(conn)

    n_raw = len(cands)
    if not args.include_virtual:
        cands = [g for g in cands if not is_virtual(g["league"])]
        print(f"排除虚拟电子足球: {n_raw - len(cands)} 场(仍保留在 matches)")
    if args.limit:
        cands = cands[: args.limit]

    print(f"待补档场次: {len(cands)}")

    # 分类统计
    stat = Counter()
    by_league = Counter()
    for g in cands:
        by_league[g["league"]] += 1
        if has_halftime_line(conn, g["home"], g["away"]):
            stat["含半场盘口(旧代码必崩)"] += 1
        elif has_any_odds(conn, g["home"], g["away"]):
            stat["有赔率-错过归档窗口"] += 1
        else:
            stat["无赔率快照"] += 1
        if g["ht_score_home"] is not None:
            stat["带半场比分"] += 1
    conn.close()

    print("\n--- 成因分布 ---")
    for k, v in stat.most_common():
        print(f"  {k:<24s} {v}")
    print("\n--- 联赛 TOP15 ---")
    for lg, n in by_league.most_common(15):
        print(f"  {lg:<36s} {n}")

    if not args.apply:
        print("\n[DRY-RUN] 未写库。确认后加 --apply")
        return

    # ── 落库 ──
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = f"{DB}.bak_{ts}"
    print(f"\n备份 {DB} -> {bak} ...")
    shutil.copy2(DB, bak)
    print(f"备份完成 ({os.path.getsize(bak) / 1e9:.2f} GB)")

    from gq.db import record_match_outcome  # 延迟导入, 避免 dry-run 触发建表

    ok = skip = fail = 0
    with open(AUDIT, "a", encoding="utf-8") as af:
        for i, g in enumerate(cands, 1):
            try:
                rec = record_match_outcome(
                    mid=str(g["mid"]),
                    home=g["home"],
                    away=g["away"],
                    league=g["league"] or "",
                    kickoff=g["kickoff"] or "",
                    score_home=g["score_home"],
                    score_away=g["score_away"],
                    ht_score_home=g["ht_score_home"],
                    ht_score_away=g["ht_score_away"],
                )
                if rec:
                    ok += 1
                    af.write(
                        json.dumps(
                            {
                                "mid": g["mid"],
                                "league": g["league"],
                                "home": g["home"],
                                "away": g["away"],
                                "kickoff": g["kickoff"],
                                "ft": [g["score_home"], g["score_away"]],
                                "ht": [g["ht_score_home"], g["ht_score_away"]],
                                "tool": "backfill_outcomes.py",
                                "ts": ts,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                else:
                    skip += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[FAIL] mid={g['mid']} {g['home']} vs {g['away']}: {e!r}")
            if i % 200 == 0:
                print(f"  ...{i}/{len(cands)}  ok={ok} skip={skip} fail={fail}")

    print(f"\n补档完成: 写入 {ok} / 跳过 {skip} / 失败 {fail}")
    print(f"备份: {bak}")
    print(f"审计: {AUDIT}")


if __name__ == "__main__":
    main()
