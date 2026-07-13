"""
data_quality_checks — 数据质量门禁 (E4+E5 P2-18, CI 可调用).

只读检查 football_data.db 的赔率数据完整性, 返回非零退出码以便 CI 阻断:
  - 空赔率: home/draw/away_odds 为 NULL 或 <= 0 (致命)
  - 异常返还率: return_rate < 1.0 (抽水为负, 数据损坏)
  - 跨书覆盖: 单场 provider 数 < MIN_PROVIDERS 的占比 (警告)
  - CS 覆盖: 含 odds_cs 的列覆盖 (信息)

用法:
  python scripts/data_quality_checks.py [--db data/football_data.db] [--fail-on warning|critical]
退出码: 0=通过, 1=仅警告, 2=存在致命项.
"""
import argparse
import os
import sqlite3
import sys
import json


def check(db_path: str, min_providers: int = 2) -> dict:
    if not os.path.exists(db_path):
        return {"ok": False, "fatal": [f"DB 不存在: {db_path}"], "warn": [], "info": {}}
    con = sqlite3.connect(db_path)
    try:
        fatal, warn, info = [], [], {}

        # 1) 空赔率 (致命)
        null_odds = con.execute(
            "SELECT COUNT(*) FROM odds WHERE home_odds IS NULL OR draw_odds IS NULL "
            "OR away_odds IS NULL OR home_odds<=0 OR draw_odds<=0 OR away_odds<=0"
        ).fetchone()[0]
        if null_odds:
            fatal.append(f"odds 表存在 {null_odds} 行空/非正赔率 (home/draw/away)")

        # 2) 异常返还率:
        #    return_rate 为赔付率 = 1/overround. 正常市场恒 <1 (庄家抽水).
        #    致命 = return_rate<=0 (不可能值, 录入/符号错误 → 数据损坏).
        #    警告 = return_rate>1.0 (overround<1, 跨书套利/margin<0, 罕见但合法, 需复核).
        bad_rr = con.execute(
            "SELECT COUNT(*) FROM odds WHERE return_rate IS NOT NULL AND return_rate <= 0"
        ).fetchone()[0]
        if bad_rr:
            fatal.append(f"odds 表存在 {bad_rr} 行 return_rate<=0 (不可能值/数据损坏)")
        arb = con.execute(
            "SELECT COUNT(*) FROM odds WHERE return_rate IS NOT NULL AND return_rate > 1.0"
        ).fetchone()[0]
        if arb:
            warn.append(f"odds 表存在 {arb} 行 return_rate>1.0 (跨书套利/margin<0, 需复核)")

        # 3) 跨书覆盖 (警告)
        total = con.execute("SELECT COUNT(*) FROM odds").fetchone()[0]
        if total:
            thin = con.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT match_id FROM odds GROUP BY match_id HAVING COUNT(DISTINCT provider) < ?"
                ") t", (min_providers,)
            ).fetchone()[0]
            coverage = 1.0 - (thin / total if total else 0)
            info["match_coverage_ge_%d_providers" % min_providers] = round(coverage, 4)
            if thin:
                warn.append(f"{thin} 场比赛 provider 数 < {min_providers} (跨书覆盖不足)")

        # 4) CS 覆盖 (信息)
        cs_tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('betting_markets','odds_cs')"
        ).fetchall()]
        info["cs_tables_present"] = cs_tables
        if "betting_markets" in cs_tables:
            bm = con.execute("SELECT COUNT(*) FROM betting_markets").fetchone()[0]
            info["betting_markets_rows"] = bm
            if bm < 100:
                warn.append(f"betting_markets 仅 {bm} 行 (CS 源稀疏, 波胆 EV 不可算)")
    finally:
        con.close()

    ok = len(fatal) == 0
    return {"ok": ok, "fatal": fatal, "warn": warn, "info": info}


def main():
    ap = argparse.ArgumentParser()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--db", default=os.path.join(root, "data", "football_data.db"))
    ap.add_argument("--fail-on", choices=["warning", "critical"], default="critical")
    ap.add_argument("--min-providers", type=int, default=2)
    args = ap.parse_args()

    res = check(args.db, args.min_providers)
    print(json.dumps(res, ensure_ascii=False, indent=2))

    if not res["ok"]:
        print("\n[FATAL] 数据质量门禁未通过:", res["fatal"], file=sys.stderr)
        sys.exit(2)
    if res["warn"] and args.fail_on == "warning":
        print("\n[WARN] 数据质量存在警告项:", res["warn"], file=sys.stderr)
        sys.exit(1)
    print("\n[OK] 数据质量门禁通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
