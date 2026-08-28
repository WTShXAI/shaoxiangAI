# -*- coding: utf-8 -*-
"""
用「时序 holdout」锁定混合权重 w  (优化方案 Fix2)
================================================
- holdout: match_outcomes.kickoff >= SPLIT (未来窗口, 与训练期不重叠)
- 对每个 w∈[0,1] 测 AUC/Acc/LogLoss, 选 AUC 最优 (tie-break Acc)
- 注意: live_1x2_model 训练时可能见过该窗口(模型拟合级泄漏), 故 w 锁定在
  时序 holdout 上, 但属「meta-权重」估计; 完整无泄漏需重训 live 模型(后续)。
"""
import os, json, sqlite3
import numpy as np
import unified_corrected_duel as U

SPLIT = "2026-08-14"   # holdout 起始(含)

def fetch_inplay_holdout(split):
    con = sqlite3.connect(U.DB_INPLAY); con.execute("PRAGMA busy_timeout=30000"); cur = con.cursor()
    cur.execute("SELECT home||' vs '||away, result, kickoff FROM match_outcomes "
                "WHERE result IN ('home','draw','away') AND kickoff >= ?", (split,))
    mo = {r[0]: U.LIDX[r[1][0].upper()] for r in cur.fetchall()}
    q = """
        SELECT match_key, minute_at, score_at,
               AVG(CASE WHEN selection='home' THEN odds END) AS h,
               AVG(CASE WHEN selection='draw' THEN odds END) AS d,
               AVG(CASE WHEN selection='away' THEN odds END) AS a
        FROM odds_snapshots
        WHERE market='1X2' AND minute_at>0 AND selection IN ('home','draw','away')
        GROUP BY match_key, minute_at, score_at
        HAVING h IS NOT NULL AND d IS NOT NULL AND a IS NOT NULL
    """
    rows = cur.execute(q).fetchall(); con.close()
    out = []
    for mk, minute, score_at, h, d, a in rows:
        if mk not in mo: continue
        if not (1 <= minute <= 95): continue
        if not (h > 1.01 and d > 1.01 and a > 1.01): continue
        sh = sa = 0
        if score_at and '-' in score_at:
            try: sh, sa = (int(x) for x in score_at.split('-'))
            except Exception: sh = sa = 0
        out.append(dict(y=mo[mk], h=h, d=d, a=a, sh=sh, sa=sa, minute=int(minute)))
    return out

def main():
    rows = fetch_inplay_holdout(SPLIT)
    y = np.array([x["y"] for x in rows])
    S = []; N = []
    for x in rows:
        sp = U.sys_inplay(x["h"], x["d"], x["a"], x["sh"], x["sa"], x["minute"])
        S.append(sp if sp else [1/3, 1/3, 1/3])
        N.append(U.naive_probs(x["h"], x["d"], x["a"]))
    S = np.array(S, dtype=float); N = np.array(N, dtype=float)
    print(f"holdout: kickoff>={SPLIT}  n={len(rows)}")
    print(f"{'w':>5}{'AUC':>9}{'Acc':>8}{'LogLoss':>10}{'Brier':>9}")
    best = None
    results = []
    for w in [i/10 for i in range(11)]:
        B = w*S + (1-w)*N
        B = B / B.sum(axis=1, keepdims=True)
        m = U.metrics(y, B)
        print(f"{w:>5.1f}{m['auc']:>9.4f}{m['acc']:>8.3f}{m['logloss']:>10.4f}{m['brier']:>9.4f}")
        results.append(dict(w=round(w,2), auc=round(m['auc'],4), acc=round(m['acc'],4),
                           logloss=round(m['logloss'],4), brier=round(m['brier'],4)))
        if best is None or m['auc'] > best[1]['auc']:
            best = (w, m)
    print(f"\nLOCKED w={best[0]:.1f}  AUC={best[1]['auc']:.4f}  Acc={best[1]['acc']:.3f}  LL={best[1]['logloss']:.4f}")
    outp = os.path.join(U.ROOT, "deliverables", "lock_w_holdout_result.json")
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(dict(split=SPLIT, n=len(rows), best_w=round(best[0],2),
                       sweep=results), f, ensure_ascii=False, indent=2)
    print(f"[ok] -> {outp}")

if __name__ == "__main__":
    main()
