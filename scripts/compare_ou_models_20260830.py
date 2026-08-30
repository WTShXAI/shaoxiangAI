"""OU 三方案对比 (2026-08-30): 现有 OU 模型 vs 市场隐含 vs Poisson GBM λ。

背景: 现有 analysis/ou_opening_model.json 是 3 特征逻辑回归, 实测
      AUC 0.5003(=抛硬币) 且劣于 naive(市场隐含概率 0.5333), 实盘 ROI -8.03%。
      本脚本检验用新训的 Poisson GBM λ 导出的 P(over) 是否能改善。

三方案:
  A 现有 OU 模型  → ou_opening_model.json (features: implied_p_over, line, league_prior)
  B naive         → 市场去水隐含 P(over)
  C GBM λ         → poisson_gbm.predict_lambdas → p_over(line)

⚠ 只在干净子集评测(排除 score_missing=1 / 从未有过比分快照的假 0-0)。

用法: runpy scripts/compare_ou_models_20260830.py [样本场数]
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "data", "events.db")
from analysis.live_goal_probe import _parse_kickoff, _open_1x2_from_snapshots  # noqa: E402
from pipeline.poisson_gbm import available as gbm_ok, predict_lambdas, p_over  # noqa: E402

OUM_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "analysis", "ou_opening_model.json")


def opening_ou(con, mk):
    """开盘主盘 OU (与 audit_ou_trust_20260829.opening_ou 同口径)。"""
    try:
        w = ("match_key=? AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' "
             "AND market NOT LIKE '%_2H%' AND odds IS NOT NULL AND odds>1.01 AND odds<1000.0")
        ps = [mk]
        t0 = con.execute(
            "SELECT MIN(minute_at) FROM odds_snapshots WHERE match_key=? "
            "AND market LIKE 'OU_%' AND market NOT LIKE '%_1H%' AND market NOT LIKE '%_2H%'",
            (mk,)).fetchone()
        m0 = int(t0[0]) if (t0 and t0[0] is not None) else None
        if m0 is not None and m0 <= 5:
            w += " AND minute_at<=?"; ps.append(m0 + 1)
        else:
            w += " AND minute_at=0"
        bkt = con.execute(f"SELECT MIN(CAST(captured_at/60 AS INTEGER)) FROM odds_snapshots WHERE {w}",
                          tuple(ps)).fetchone()
        if not bkt or bkt[0] is None:
            return None
        rows = con.execute(f"SELECT market, selection, odds FROM odds_snapshots WHERE {w} "
                           f"AND CAST(captured_at/60 AS INTEGER)=?", tuple(ps) + (bkt[0],)).fetchall()
    except Exception:
        return None
    d = {}
    for mkt, sel, o in rows:
        d.setdefault(mkt, {})[str(sel).lower()] = float(o)
    best = None
    for mkt, v in d.items():
        ov, un = v.get('over'), v.get('under')
        if not ov or not un:
            continue
        try:
            line = float(str(mkt).split('_', 2)[-1])
        except Exception:
            continue
        ovr = 1.0 / ov + 1.0 / un
        if best is None or ovr < best[3]:
            best = (line, ov, un, ovr)
    return best[:3] if best else None


def ou_model_prob(M, implied, line, league):
    """现有 OU 模型: logistic(β·[implied, line, league_prior] + b)。"""
    try:
        priors = M.get('league_priors') or {}
        lp = priors.get(league, M.get('global_over_rate', 0.5))
        coef = M['coef']; b = M['intercept']
        z = coef[0] * implied + coef[1] * line + coef[2] * lp + b
        return 1.0 / (1.0 + math.exp(-max(min(z, 30), -30)))
    except Exception:
        return None


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2500
    M = json.load(open(OUM_PATH, encoding='utf-8'))
    con = sqlite3.connect(DB, timeout=60)
    rows = con.execute(
        "SELECT match_key, home, away, score_home, score_away, kickoff, league FROM matches "
        "WHERE status='finished' AND score_home IS NOT NULL AND kickoff>='2026-08-15' "
        "ORDER BY kickoff DESC LIMIT ?", (n,)).fetchall()

    rec = []
    skipped = {'fake': 0, 'no_ou': 0, 'no_1x2': 0, 'gbm_fail': 0}
    for mk, home, away, sh, sa, ko, league in rows:
        if con.execute("SELECT 1 FROM matches WHERE match_key=? AND score_missing=1",
                       (mk,)).fetchone():
            skipped['fake'] += 1; continue
        if not con.execute("SELECT 1 FROM odds_snapshots WHERE match_key=? AND score_at IS NOT NULL "
                           "AND score_at!='' AND score_at!='0-0' LIMIT 1", (mk,)).fetchone():
            skipped['fake'] += 1; continue
        ou = opening_ou(con, mk)
        if not ou:
            skipped['no_ou'] += 1; continue
        line, ov, un = ou
        try:
            oh, od, oa = _open_1x2_from_snapshots(con, mk)
        except Exception:
            oh = od = oa = None
        if not (oh and od and oa and oh > 1.01 and od > 1.01 and oa > 1.01):
            skipped['no_1x2'] += 1; continue
        lam = predict_lambdas(oh, od, oa, ch=oh, cd=od, ca=oa, league=league) if gbm_ok() else None
        if not lam:
            skipped['gbm_fail'] += 1; continue

        implied = (1.0 / ov) / ((1.0 / ov) + (1.0 / un))
        pA = ou_model_prob(M, implied, line, league)
        pB = implied
        pC = p_over(lam[0], lam[1], line)
        if pA is None:
            continue
        y = 1 if (int(sh) + int(sa)) > line else 0
        rec.append((y, pA, pB, pC, ov if y else 0.0, un if not y else 0.0))

    print(f"跳过: {skipped} | 有效样本 {len(rec)}")
    if not rec:
        return

    def auc(scores, ys):
        pos = [s for s, y in zip(scores, ys) if y == 1]
        neg = [s for s, y in zip(scores, ys) if y == 0]
        if not pos or not neg:
            return float('nan')
        tot = 0.0
        for p in pos:
            for q in neg:
                tot += 1.0 if p > q else (0.5 if p == q else 0.0)
        return tot / (len(pos) * len(neg))

    ys = [r[0] for r in rec]
    print(f"\n{'方案':<26}{'Brier(低好)':>12}{'LogLoss':>10}{'AUC(高好)':>11}{'ROI':>9}")
    print("-" * 68)
    for i, tag in enumerate(("A 现有OU模型", "B naive(市场隐含)", "C GBM λ → P(over)"), 1):
        ps = [r[i] for r in rec]
        br = sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys)
        ll = -sum(math.log(max(p if y else 1 - p, 1e-9))
                  for p, y in zip(ps, ys)) / len(ys)
        a = auc(ps, ys)
        # ROI: 只在 |p-implied|>0.05 时下注
        stake = ret = 0.0
        for r in rec:
            y, p = r[0], r[i]
            imp = r[2]
            if p - imp > 0.05:
                stake += 1; ret += r[4]
            elif imp - p > 0.05:
                stake += 1; ret += r[5]
        roi = (ret - stake) / stake * 100 if stake > 0 else 0.0
        print(f"{tag:<26}{br:>12.4f}{ll:>10.4f}{a:>11.4f}{roi:>+8.2f}%")

    print("\n判据: Brier/LogLoss 越低越好, AUC 越高越好; "
          "AUC<=0.5 = 无判别力(等同抛硬币)。")
    con.close()


if __name__ == "__main__":
    main()
