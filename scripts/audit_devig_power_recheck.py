"""去水口径复核: 用原始赔率 + 幂法(devig_power, 抑 FLB) 重算 KNN EDGE。

承接 scripts/audit_knn_edge_devig.py 的结论:
  比例法(devig_n)下"无脑买热门"在 2562 场上 ROI +5.15%(CI下限>0),
  与 KNN 的 +5.86% 不可分辨(配对差 +0.71pp, CI含0)。

本脚本解决该检验的残余疑问: 比例法是否有偏? 换成幂法后还剩多少?

方法:
  1. 从 events.db.odds_snapshots 取每场开赛前(captured_at<=kickoff)最后一组 1X2 原始赔率
  2. 自校验: 比例法重算的去水赔率 vs 账本 devig_h/d/a (吻合 → 原始赔率口径正确)
  3. 同一批原始赔率下同时算 devig_n(比例) / devig_power(幂法)
  4. 两种口径各算: KNN ROI / 无脑买热门 ROI / 配对差(KNN-热门)

只读 events.db + verification.db; 输出 reports/devig_power_recheck.json
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.odds_math import devig_n, devig_power  # SSoT

EV = ROOT / "data" / "events.db"
VD = ROOT / "verification.db"
OUT = ROOT / "reports" / "devig_power_recheck.json"


def parse_ts(s: str):
    """ISO8601 / 'YYYY-MM-DD HH:MM' → unix ts (UTC)。失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def roi(xs):
    return sum(xs) / len(xs) if xs else None


def ci95(xs, n_boot=4000, seed=11):
    if len(xs) < 5:
        return None
    rng = random.Random(seed)
    n = len(xs)
    ms = []
    for _ in range(n_boot):
        ms.append(sum(xs[rng.randrange(n)] for _ in range(n)) / n)
    ms.sort()
    return [ms[int(0.025 * n)], ms[int(0.975 * n) - 1]]


def main():
    vc = sqlite3.connect(f"file:{VD}?mode=ro", uri=True)
    vc.row_factory = sqlite3.Row
    knn = [
        dict(r)
        for r in vc.execute(
            "select * from verification_ledger where model_source='KNN' "
            "and settled_outcome is not null"
        )
    ]
    vc.close()

    ec = sqlite3.connect(f"file:{EV}?mode=ro", uri=True)
    q = (
        "select selection, odds, captured_at from odds_snapshots "
        "where match_key=? and market='1X2' and captured_at<=? "
        "order by captured_at desc limit 60"
    )

    matched = 0
    recs = []
    for r in knn:
        kts = parse_ts(r["kickoff_utc"])
        if kts is None:
            continue
        trio = {}
        for sel, o, ts in ec.execute(q, (r["match_id"], kts)):
            if sel in ("home", "draw", "away") and sel not in trio:
                trio[sel] = float(o)
        if len(trio) != 3:
            continue
        raw = [trio["home"], trio["draw"], trio["away"]]
        pn = devig_n(raw)
        pp = devig_power(raw)
        if not pn or not pp:
            continue
        dn = [1.0 / p for p in pn]
        dp = [1.0 / p for p in pp]
        # 自校验: 比例法重算 vs 账本
        dev = max(
            abs(dn[0] - r["devig_h"]), abs(dn[1] - r["devig_d"]), abs(dn[2] - r["devig_a"])
        )
        if dev < 0.02:
            matched += 1
        recs.append(
            {
                "m": r,
                "raw": raw,
                "dn": {"home": dn[0], "draw": dn[1], "away": dn[2]},
                "dp": {"home": dp[0], "draw": dp[1], "away": dp[2]},
                "dev": dev,
            }
        )
    ec.close()

    out = {
        "knn_settled": len(knn),
        "raw_odds_resolved": len(recs),
        "proportional_devig_match_ledger": matched,
        "match_rate": (matched / len(recs)) if recs else None,
    }

    def payoff(d, outcome, win):
        return (d[outcome] - 1.0) if win == outcome else -1.0

    def evaluate(key):
        knn_p, fav_p, diff = [], [], []
        for x in recs:
            r = x["m"]
            d = x[key]
            win = r["settled_outcome"]
            knn_p.append(payoff(d, r["chosen_outcome"], win))
            sh = min(d, key=lambda k: d[k])
            fp = payoff(d, sh, win)
            fav_p.append(fp)
            diff.append(knn_p[-1] - fp)
        return {
            "knn": {"roi": roi(knn_p), "ci95": ci95(knn_p)},
            "always_favorite": {"roi": roi(fav_p), "ci95": ci95(fav_p)},
            "paired_diff": {"mean": roi(diff), "ci95": ci95(diff)},
        }

    out["devig_n_proportional"] = evaluate("dn")
    out["devig_power"] = evaluate("dp")

    # 幂法 vs 比例法 对热门赔率的影响 (平均抬高幅度)
    lifts = []
    for x in recs:
        sh = min(x["dn"], key=lambda k: x["dn"][k])
        lifts.append((x["dn"][sh] - x["dp"][sh]) / x["dp"][sh])
    out["fav_odds_inflation_proportional_vs_power"] = {
        "mean_relative": sum(lifts) / len(lifts) if lifts else None,
        "n": len(lifts),
    }

    a = out["devig_n_proportional"]
    b = out["devig_power"]
    out["verdict"] = {
        "power_kills_favorite_bias": bool(
            a["always_favorite"]["roi"] and b["always_favorite"]["roi"] is not None
            and b["always_favorite"]["roi"] < a["always_favorite"]["roi"]
        ),
        "knn_excess_over_favorite_power_ci_contains_zero": bool(
            b["paired_diff"]["ci95"]
            and b["paired_diff"]["ci95"][0] <= 0 <= b["paired_diff"]["ci95"][1]
        ),
        "note": "两项皆真 → KNN EDGE 为去水伪影, 修正口径后无可分辨超额",
    }

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
