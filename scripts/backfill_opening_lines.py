# -*- coding: utf-8 -*-
"""
backfill_opening_lines.py — 用 SSoT 重建的初盘主线回填 match_outcomes
=====================================================================

背景 (2026-08-05 取证):
  1) OU「最小线 bug」只修了代码 (gq/db.py), 历史数据从未回填。
     match_outcomes.op_ou_line 众数 = 1.5(413) / 0.5(322)
     opening_line 重建主盘 众数 = 2.75(473) / 2.5(457)
     实测 2277/2670 = 85.3% 的场次盘口线是错的。
     26 个下游脚本直接读 op_ou_line -> 全部吃到错线。

  2) AH「line=0 伪造」: 采集器 parse_ah_line('') 返回 0.0, 把缺失让球线
     捏造成平手盘。时间线零重叠铁证:
        非零 AH line: captured_at ∈ [1784046944, 1784063064]
                      = 2026-07-15 00:35 ~ 05:04  (仅 4.5 小时窗口)
        line=0.0    : captured_at ∈ [1784325860, 1785849600]
                      = 2026-07-18 06:04 ~ 2026-08-04 21:20
     两段完全不相交 => op_ah_line = 0 的 1219 场 100% 是伪造。
     按铁律1(未知一律 --, 绝不填 0 假装已知) 必须置 NULL。

安全设计:
  - 默认 dry-run, 只打印影响面, 不写库
  - --apply 才写库, 写前把所有被改行的原值存成审计 JSON
  - match_key (home||' vs '||away) 有重复的场次一律跳过 (避免错配串行)
  - 只改 op_ou_line/op_ou_over/op_ou_under 与 op_ah_line, 不碰赛果

用法:
    python scripts/backfill_opening_lines.py            # dry-run
    python scripts/backfill_opening_lines.py --apply    # 写库 + 审计
"""
from __future__ import annotations

import os
import sys
import json
import time
import sqlite3
import argparse
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GQ_DB = os.path.join(ROOT, "data", "events.db")
AUDIT_DIR = os.path.join(ROOT, "data", "audit")

# AH 污染期起点: 2026-07-18 06:04:20 (采集器 parse_ah_line 回归上线)
AH_CORRUPTION_EPOCH = 1784325860


def load_match_outcomes(con) -> tuple[dict, set]:
    """返回 {match_key: row} 与 重复 match_key 集合。"""
    rows = con.execute("""
        SELECT rowid, home, away, op_ou_line, op_ou_over, op_ou_under,
               op_ah_line, op_ah_home, op_ah_away, source, is_valid
        FROM match_outcomes
    """).fetchall()
    by_key = defaultdict(list)
    for r in rows:
        by_key[f"{r[1]} vs {r[2]}"].append(r)
    dup = {k for k, v in by_key.items() if len(v) > 1}
    uniq = {k: v[0] for k, v in by_key.items() if len(v) == 1}
    return uniq, dup


def plan_ou_backfill(con) -> tuple[list, dict]:
    """返回 [(rowid, line, over, under, old_line)] 与统计。"""
    from pipeline.opening_line import build_opening_lines

    op = build_opening_lines(market="OU")
    uniq, dup = load_match_outcomes(con)

    updates, stat = [], Counter()
    stat["rebuilt"] = len(op)
    stat["dup_keys"] = len(dup)

    for _, r in op.iterrows():
        mk = r["match_key"]
        if mk in dup:
            stat["skip_dup"] += 1
            continue
        row = uniq.get(mk)
        if row is None:
            stat["skip_nomatch"] += 1
            continue
        new_line = round(float(r["line"]), 2)
        new_over = round(float(r["over"]), 4)
        new_under = round(float(r["under"]), 4)
        old_line = row[3]
        if old_line is not None and abs(float(old_line) - new_line) < 1e-6:
            stat["same"] += 1
            continue
        stat["fill" if old_line is None else "fix"] += 1
        updates.append((row[0], new_line, new_over, new_under, old_line,
                        row[4], row[5], mk))
    return updates, stat


def plan_ah_purge(con) -> tuple[list, dict]:
    """op_ah_line = 0 的记录一律置 NULL (伪造平手盘)。"""
    rows = con.execute("""
        SELECT rowid, home, away, op_ah_line, op_ah_home, op_ah_away
        FROM match_outcomes WHERE op_ah_line = 0
    """).fetchall()
    stat = Counter()
    stat["purge"] = len(rows)
    # 反查这批场次在 odds_snapshots 里是否真有非零 AH (若有则不该清)
    keep = []
    for r in rows:
        mk = f"{r[1]} vs {r[2]}"
        n = con.execute("""
            SELECT COUNT(*) FROM odds_snapshots
            WHERE match_key = ? AND market LIKE 'AH%' AND line IS NOT NULL AND line <> 0
        """, (mk,)).fetchone()[0]
        if n > 0:
            stat["has_real_nonzero"] += 1
            continue
        keep.append((r[0], mk, r[3], r[4], r[5]))
    return keep, stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库 (默认 dry-run)")
    ap.add_argument("--db", default=GQ_DB)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)

    print("=" * 74)
    print("A) OU 主盘线回填 (最小线 bug 遗留数据修正)")
    print("=" * 74)
    ou_updates, ou_stat = plan_ou_backfill(con)
    print(f"  opening_line 重建     : {ou_stat['rebuilt']} 场")
    print(f"  match_key 重复(跳过)  : {ou_stat['dup_keys']} 个键 / 跳过 {ou_stat['skip_dup']} 场")
    print(f"  match_outcomes 无此场 : {ou_stat['skip_nomatch']}")
    print(f"  现值已正确(不动)      : {ou_stat['same']}")
    print(f"  现值为空 -> 填充      : {ou_stat['fill']}")
    print(f"  现值错误 -> 修正      : {ou_stat['fix']}")
    print(f"  合计待写              : {len(ou_updates)}")

    if ou_updates:
        old_c = Counter(round(float(u[4]), 2) for u in ou_updates if u[4] is not None)
        new_c = Counter(u[1] for u in ou_updates)
        print(f"\n  旧线众数 : {old_c.most_common(6)}")
        print(f"  新线众数 : {new_c.most_common(6)}")
        shift = [u[1] - float(u[4]) for u in ou_updates if u[4] is not None]
        if shift:
            print(f"  平均线位移 : {sum(shift)/len(shift):+.3f} (正=旧线被低估)")

    print("\n" + "=" * 74)
    print("B) AH 伪造平手盘清洗 (op_ah_line = 0 -> NULL)")
    print("=" * 74)
    ah_purge, ah_stat = plan_ah_purge(con)
    print(f"  op_ah_line = 0 的场次     : {ah_stat['purge']}")
    print(f"  实际有非零 AH 快照(保留)  : {ah_stat['has_real_nonzero']}")
    print(f"  确认伪造 -> 置 NULL       : {len(ah_purge)}")

    if not args.apply:
        print("\n[DRY-RUN] 未写库。确认无误后加 --apply 执行。")
        con.close()
        return

    # ---------------- 写库 ----------------
    os.makedirs(AUDIT_DIR, exist_ok=True)
    ts = int(time.time())
    audit = {
        "ts": ts,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": args.db,
        "reason": "OU 最小线bug遗留回填 + AH 伪造平手盘清洗 (铁律1)",
        "ou_updates": [
            {"rowid": u[0], "match_key": u[7],
             "old": {"line": u[4], "over": u[5], "under": u[6]},
             "new": {"line": u[1], "over": u[2], "under": u[3]}}
            for u in ou_updates
        ],
        "ah_purge": [
            {"rowid": p[0], "match_key": p[1],
             "old": {"line": p[2], "home": p[3], "away": p[4]},
             "new": {"line": None}}
            for p in ah_purge
        ],
    }
    audit_path = os.path.join(AUDIT_DIR, f"backfill_opening_lines_{ts}.json")
    with open(audit_path, "w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=1)
    print(f"\n审计已存: {audit_path}")

    cur = con.cursor()
    cur.executemany(
        "UPDATE match_outcomes SET op_ou_line=?, op_ou_over=?, op_ou_under=? WHERE rowid=?",
        [(u[1], u[2], u[3], u[0]) for u in ou_updates])
    cur.executemany(
        "UPDATE match_outcomes SET op_ah_line=NULL WHERE rowid=?",
        [(p[0],) for p in ah_purge])
    con.commit()
    print(f"已写库: OU {len(ou_updates)} 行, AH 清洗 {len(ah_purge)} 行")

    # 写后复核
    r = cur.execute("""
        SELECT SUM(op_ou_line IS NULL), SUM(op_ah_line IS NULL), SUM(op_ah_line = 0)
        FROM match_outcomes
    """).fetchone()
    print(f"复核: ou_null={r[0]}  ah_null={r[1]}  ah_zero={r[2]} (应为 0)")
    print("  op_ou_line 新分布 (top8):")
    for ln, c in cur.execute("""
        SELECT op_ou_line, COUNT(*) FROM match_outcomes
        WHERE op_ou_line IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 8
    """):
        print(f"    {ln:>6} : {c}")
    con.close()


if __name__ == "__main__":
    main()
