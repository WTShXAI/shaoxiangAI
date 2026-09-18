"""H1 paper-trading 台账 (哨响AI).

扫描 GQ 当前 scheduled (未开赛) 比赛, 取开盘 1X2, 用 H1 检测器
(Dixon-Coles 队力公平概率 vs 开盘去水隐含概率) 打偏差分, 输出 flag 台账.

铁律:
- 仅用赛前信息 (开盘1X2, 不开盘后/滚球价).
- 未知队/联赛回退 league_avg, 仍无法定 fair 则 SKIP 并标注.
- 仅生成 flag, 不下注; 真钱前先 paper-trading 验证 detector ROI.

用法: python analysis/h1_paper_trading.py [--buffer 0.015] [--league 中超]
"""
from __future__ import annotations
import os, json, sqlite3, argparse
from datetime import datetime
import sys
sys.path.insert(0, "D:/Architecture/analysis")
from h1_fav_undervalue_detector import get_or_build_bank, detect

GQ = "D:/Architecture/data/events.db"
OUT = "D:/Architecture/analysis/paper_trading_h1.jsonl"


def opening_1x2(g, match_key):
    """取该场开盘 1X2 (minute_at=0, 最早 captured_at 的 home/draw/away)."""
    rows = g.execute(
        "SELECT selection,odds,captured_at FROM odds_snapshots "
        "WHERE market='1X2' AND match_key=? AND minute_at=0 ORDER BY captured_at",
        (match_key,)).fetchall()
    best = {}
    for sel, odds, cap in rows:
        if sel not in best:
            try:
                best[sel] = float(odds)
            except (TypeError, ValueError):
                pass
    if set(best) >= {"home", "draw", "away"}:
        return best["home"], best["draw"], best["away"]
    return None


def main(buffer=0.015, league=None, only_flag=True):
    bank = get_or_build_bank()
    g = sqlite3.connect(GQ)
    g.row_factory = sqlite3.Row
    q = """SELECT home,away,league,kickoff FROM matches
           WHERE status='scheduled'
             AND EXISTS(SELECT 1 FROM odds_snapshots o WHERE o.match_key=home||' vs '||away AND o.market='1X2' AND o.minute_at=0)"""
    params = []
    if league:
        q += " AND league=?"
        params.append(league)
    rows = g.execute(q, params).fetchall()
    g.close()

    flags = []
    skipped = 0
    for r in rows:
        mk = f"{r['home']} vs {r['away']}"
        o = opening_1x2(sqlite3.connect(GQ), mk)
        if o is None:
            skipped += 1
            continue
        oh, od, oa = o
        d = detect(bank, r["league"], r["home"], r["away"], oh, od, oa, buffer)
        if d is None:
            skipped += 1
            continue
        d["kickoff"] = r["kickoff"]
        d["match_key"] = mk
        d["generated_at"] = datetime.now().isoformat(timespec="seconds")
        if only_flag and not d["undervalued"]:
            continue
        flags.append(d)

    # 写台账 (覆盖式, 保留历史在 .bak)
    if os.path.exists(OUT):
        os.replace(OUT, OUT + ".bak")
    with open(OUT, "w", encoding="utf-8") as fh:
        for d in flags:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    print(f"[paper-trading] 扫描 scheduled={len(rows)} 命中flag={len(flags)} 跳过(无fair/无开盘)={skipped}")
    print(f"  台账已写 {OUT}")
    for d in sorted(flags, key=lambda x: -x["edge"])[:15]:
        print(f"  {d['league']} {d['home']} vs {d['away']}: "
              f"fav={d['favorite']} open={d['favorite_open']} edge={d['edge']:+.3f} "
              f"fair={d['fair_fav']:.3f} vs open_imp={d['open_implied_fav']:.3f} "
              f"[{d['fair_source']}] kelly={d['kelly']:.3f}")
    return flags


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--buffer", type=float, default=0.015)
    ap.add_argument("--league", type=str, default=None)
    ap.add_argument("--all", action="store_true", help="输出全部(含PASS), 不只flag")
    a = ap.parse_args()
    main(buffer=a.buffer, league=a.league, only_flag=not a.all)
