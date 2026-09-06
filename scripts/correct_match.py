# -*- coding: utf-8 -*-
"""人工纠偏锁 · 自动化工具 (完整方案)

用法:
  # 锁定某场可信终场 (采集器后续永不覆盖)
  python scripts/correct_match.py --home 伊比利亚1999 --away 拉恩 \
      --status finished --sh 2 --sa 2 --ht-sh 1 --ht-sa 1 --source "user-ground-truth" --mid 5559327

  # 仅按队名锁定 (mid 未知时)
  python scripts/correct_match.py --home 甲队 --away 乙队 --status finished --sh 3 --sa 1

  # 列出当前所有已锁定比赛
  python scripts/correct_match.py --list

  # 防御性重断言 (每日 recheck 兜底调用): 用 override_data 重写所有锁定终场
  python scripts/correct_match.py --reassert

说明:
  - 写库经 gq.db.apply_override: 置 matches.is_override=1 + override_data(JSON 快照),
    并同步锁定 match_outcomes(若存在同 mid). 幂等, 重复调用安全.
  - 采集器 (upsert_match / _sweep_finished / record_match_outcome) 已加 is_override 守卫,
    被锁比赛永不被动覆盖 (零回归).
  - --reassert 经 gq.db.reassert_overrides 兜底重断言, 由 recheck_analysis.py 每日调用.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from gq import db as gqdb


def _resolve_match_key(home, away, match_key):
    if match_key:
        return match_key
    if not home or not away:
        raise SystemExit("--home 与 --away 必填 (或提供 --match-key)")
    return f"{home} vs {away}"


def cmd_correct(args):
    mk = _resolve_match_key(args.home, args.away, args.match_key)
    if args.status == "finished" and (args.sh is None or args.sa is None):
        raise SystemExit("status=finished 时必须提供 --sh / --sa")
    ok = gqdb.apply_override(
        mk, args.home or "", args.away or "", args.status,
        args.sh, args.sa, args.ht_sh, args.ht_sa,
        minute=args.minute, league="", source=args.source, mid=args.mid)
    if ok:
        print(f"[OK] 已锁定纠偏: {mk} -> status={args.status} "
              f"score={args.sh}-{args.sa} HT={args.ht_sh}-{args.ht_sa} "
              f"source={args.source}")
    else:
        print("[FAIL] 锁定失败")
        sys.exit(1)


def cmd_list(args):
    with gqdb.conn() as c:
        rows = c.execute(
            "SELECT match_key, status, score_home, score_away, source, override_at "
            "FROM matches WHERE is_override=1").fetchall()
    for r in rows:
        d = dict(r)
        print(f"  {d.get('match_key')} | {d.get('status')} | "
              f"{d.get('score_home')}-{d.get('score_away')} | src={d.get('source')}")


def cmd_reassert(args):
    n = gqdb.reassert_overrides()
    print(f"[OK] 重断言 {n} 场锁定比赛终场")


def main():
    # 确保 schema 最新 (新增 is_override / override_data / override_at 列)
    gqdb.init_db()
    ap = argparse.ArgumentParser(description="人工纠偏锁 · 自动化工具")
    ap.add_argument("--home")
    ap.add_argument("--away")
    ap.add_argument("--match-key")
    ap.add_argument("--status", default="finished",
                   choices=["finished", "live", "scheduled", "filtered"])
    ap.add_argument("--sh", type=int, default=None)
    ap.add_argument("--sa", type=int, default=None)
    ap.add_argument("--ht-sh", type=int, default=None, dest="ht_sh")
    ap.add_argument("--ht-sa", type=int, default=None, dest="ht_sa")
    ap.add_argument("--minute", type=int, default=90)
    ap.add_argument("--mid")
    ap.add_argument("--source", default="manual")
    ap.add_argument("--list", action="store_true", help="列出已锁定比赛")
    ap.add_argument("--reassert", action="store_true", help="防御性重断言")
    args = ap.parse_args()

    if args.list:
        cmd_list(args)
        return
    if args.reassert:
        cmd_reassert(args)
        return
    cmd_correct(args)


if __name__ == "__main__":
    main()
