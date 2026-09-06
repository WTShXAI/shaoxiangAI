"""多庄 sharp 共识 → 波胆 真实性检验试点 (N=6, GQ∩leisu)
========================================================
为什么做: 用户批准评估"IW∩leisu 重叠场能否跑真 walk-forward"。
探查结论:
  - IW 队名中文规范化 ↔ leisu 中文队名 直接可对齐(无需映射), 但 IW 数据止于 2025-04,
    leisu 2026-07 才采 -> 历史同场无重叠 -> 真 walk-forward 不可行(需前瞻积累)。
  - events.db 含 leisu 那 6 场 + 真实比分(score_home/score_away) + 单庄 1X2 ->
    三元组 (base=GQ乐鱼单庄1X2, tilt=leisu多庄sharp, eval=GQ真赛果) 现在可跑 6 场试点。
目标: 在 6 个真实赛果上, 比 OIP-base vs OIP-sharp-tilt 的 top3 命中率(机制检查, 非统计结论)。
"""
from __future__ import annotations
import sqlite3
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))

import numpy as np
from scipy.optimize import root
from scipy.stats import poisson

from pipeline.multibook_consensus import sharp_consensus_for_match

GQ = "data/events.db"


def _indep_1x2(lam, mu, maxg=8):
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    M = np.outer(h, a)
    return np.tril(M, -1).sum(), np.trace(M), np.triu(M, 1).sum()


def oip_from_1x2(ph, pd_, pa, maxg=8):
    def obj(p):
        lam, mu = p
        h, d, a = _indep_1x2(lam, mu, maxg)
        return [h - ph, d - pd_]
    sol = root(obj, [1.3, 1.1], method="hybr")
    if sol.success:
        lam, mu = max(0.05, float(sol.x[0])), max(0.05, float(sol.x[1]))
    else:
        # 网格兜底: 极端盘口 root 易失败, 粗搜最小化 |P(H)-ph|+|P(D)-pd|
        best, berr = (1.3, 1.1), 1e9
        for lam in np.linspace(0.2, 4.0, 80):
            for mu in np.linspace(0.2, 4.0, 80):
                h, d, a = _indep_1x2(lam, mu, maxg)
                err = abs(h - ph) + abs(d - pd_)
                if err < berr:
                    berr, best = err, (lam, mu)
        lam, mu = best
    h = poisson.pmf(np.arange(maxg + 1), lam)
    a = poisson.pmf(np.arange(maxg + 1), mu)
    M = np.outer(h, a)
    return M / M.sum()


def deoverround(oh, od, oa):
    inv = 1.0 / oh + 1.0 / od + 1.0 / oa
    return np.array([(1.0 / oh) / inv, (1.0 / od) / inv, (1.0 / oa) / inv])


def top3_idx(M, k=3):
    flat = M.flatten()
    return [tuple(int(x) for x in np.unravel_index(i, M.shape)) for i in np.argsort(-flat)[:k]]


def get_gq(home, away):
    g = sqlite3.connect(GQ)
    # 模糊匹配: GQ 队名可能与 leisu 略有出入, 用双向 LIKE 兜底
    r = g.execute(
        """SELECT match_key, score_home, score_away, status, kickoff
           FROM matches
           WHERE (home LIKE ? AND away LIKE ?) OR (home LIKE ? AND away LIKE ?)""",
        (f"%{home}%", f"%{away}%", f"%{away}%", f"%{home}%")).fetchone()
    g.close()
    return r


def get_gq_1x2(match_key):
    g = sqlite3.connect(GQ)
    rows = g.execute(
        "SELECT selection, odds FROM odds_snapshots WHERE match_key=? AND market='1X2'",
        (match_key,)).fetchall()
    g.close()
    m = {}
    for sel, o in rows:
        s = (sel or "").lower()
        if s in ("home", "h", "1"): m["h"] = o
        elif s in ("draw", "d", "x", "0"): m["d"] = o
        elif s in ("away", "a", "2"): m["a"] = o
    return (m["h"], m["d"], m["a"]) if len(m) == 3 else None


def main():
    leisu = sqlite3.connect("data/football_data.db")
    pairs = leisu.execute(
        """SELECT DISTINCT home_raw, away_raw FROM leisu_odds
           WHERE market='1X2' AND home_raw IS NOT NULL AND away_raw IS NOT NULL""").fetchall()
    leisu.close()

    base_hit = tilt_hit = 0
    out = []
    for home, away in pairs:
        gq = get_gq(home, away)
        if not gq:
            out.append({"home": home, "away": away, "note": "GQ 无此场(跳过)"})
            continue
        mk, sh, sa, status, kickoff = gq
        if sh is None or sa is None:
            out.append({"home": home, "away": away, "note": f"GQ 无比分(status={status}, 跳过)"})
            continue
        odds = get_gq_1x2(mk)
        if not odds:
            out.append({"home": home, "away": away, "note": "GQ 无 1X2 赔率(跳过)"})
            continue
        base_p = deoverround(*odds)
        oip = oip_from_1x2(*base_p)
        if oip is None:
            out.append({"home": home, "away": away, "note": "OIP 反解失败(跳过)"})
            continue
        cons = sharp_consensus_for_match(home, away)
        used = cons is not None
        tilt_p = np.array([cons["h"], cons["d"], cons["a"]]) if used else base_p
        # tilt: OIP 矩阵缩放到目标 1X2
        from pipeline.dc_score_model import tilt_to_outcomes
        tilted = tilt_to_outcomes(oip, tilt_p)

        actual = (int(sh), int(sa))
        base_top = top3_idx(oip)
        tilt_top = top3_idx(tilted)
        bh = actual in base_top
        th = actual in tilt_top
        base_hit += bh
        tilt_hit += th
        out.append({
            "home": home, "away": away, "actual": list(actual),
            "gq_1x2": [round(x, 2) for x in odds],
            "sharp_consensus": {k: round(v * 100, 1) for k, v in (cons or {}).items()} if used else None,
            "base_top3": [list(x) for x in base_top],
            "tilt_top3": [list(x) for x in tilt_top],
            "base_hit": bh, "tilt_hit": th, "sharp_used": used,
        })

    n = sum(1 for o in out if "actual" in o)
    summary = {
        "module": "pilot_multibook_score",
        "n_matches_with_result": n,
        "base_top3_hits": base_hit,
        "tilt_top3_hits": tilt_hit,
        "caveat": "N=6 试点, 非统计结论; 真 walk-forward 需 leisu 前瞻积累 + GQ 同场赛果",
        "matches": out,
    }
    Path("data/pilot_multibook_score_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"多庄 sharp 波胆 真实性试点 — {n} 场有真赛果")
    print(f"  base(OIP单庄) top3 命中 = {base_hit}/{n}")
    print(f"  tilt(多庄sharp) top3 命中 = {tilt_hit}/{n}")
    print("=" * 70)
    for o in out:
        if "actual" not in o:
            print(f"  {o['home']} vs {o['away']}: {o.get('note')}")
            continue
        tag = "★" if o["sharp_used"] else " "
        print(f"  {o['home']} vs {o['away']}  真赛果={o['actual']} {tag}sharp={o['sharp_consensus']}")
        print(f"    base top3={o['base_top3']} 命中={o['base_hit']}")
        print(f"    tilt top3={o['tilt_top3']} 命中={o['tilt_hit']}")
    print(f"\n已写出 data/pilot_multibook_score_report.json")


if __name__ == "__main__":
    main()
