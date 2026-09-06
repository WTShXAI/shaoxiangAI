"""
CS 信任模型可行性回测 (诚实判据, 非 top-1 骗术)
====================================================
问题: 结合初盘所有赔率, 能否建一个"用户可信"的 CS(波胆)模型?

诚实判据: 不是"猜中精确比分"(所有人~10-15%, 含庄家), 而是
  **概率校准 / log-loss** — 谁的"比分概率分布"在赛果上交叉熵更低, 谁更可信。

数据: data/events.db
  - pre_match_cs.odds_json : 初盘完整 CS 矩阵 {比分:赔率} (26 个比分)
  - match_outcomes         : op_1x2_h/d/a(初盘1X2) + op_ou_*(初盘大小球) + score_home/away(赛果)
  配对: 2541 场 (home+away+kickoff 关联)

模型:
  B  = 庄家 CS 基线 (初盘CS去水 -> 隐含比分分布)
  M1 = OIP Poisson (仅初盘1X2 反解 λ/μ -> 比分分布)
  M2 = 结合初盘1X2+OU 拟合 λ/μ + 联赛经验先验 shrinkage (贝叶斯收缩)

评估: 按 kickoff 时间切分, 前80%训练(M2先验), 后20%测试.
  指标: log_loss(↓好) / Brier(↓好) / top1命中 / 可靠性(校准).
"""
from __future__ import annotations
import sqlite3, json, math, os
import numpy as np
from scipy.optimize import least_squares
from scipy.stats import poisson

DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "events.db")
MAXG = 6  # 比分上界 0..6 (更高按 6-6 归并, 实际极少)

def demargin_cs(odds: dict) -> dict:
    inv = {s: 1.0 / o for s, o in odds.items() if o and o > 0}
    z = sum(inv.values())
    return {s: v / z for s, v in inv.items()}

def grid_to_key(i, j):
    return f"{min(i,MAXG)}-{min(j,MAXG)}"

def poisson_marginal(lh, la, maxg=MAXG):
    h = poisson.pmf(np.arange(maxg + 1), lh)
    a = poisson.pmf(np.arange(maxg + 1), la)
    M = np.outer(h, a)
    return M / M.sum()

def p_hda(M):
    pd_ = np.tril(M, -1).sum(); pp = np.trace(M); pa = np.triu(M, 1).sum()
    return np.array([pd_, pp, pa])

def fit_lambda_mu(ph, pd, pa, po=None, ou_line=2.5):
    """从初盘1X2(去水) + 可选OU(去水over概率) 拟合 λ/μ. 真正'结合初盘所有赔率'."""
    def resid(p):
        lh, la = p
        if lh <= 0 or la <= 0:
            return [1e6] * (3 if po is None else 4)
        M = poisson_marginal(lh, la)
        ph_, pd_, pa_ = p_hda(M)
        r = [ph_ - ph, pd_ - pd, pa_ - pa]
        if po is not None:
            # P(total > ou_line) via CDF
            cdf = np.cumsum(M.sum(axis=0))  # P(home<=k) marginal? need 2D
            # total > line:
            tot = 0.0
            for i in range(MAXG + 1):
                for j in range(MAXG + 1):
                    if i + j > ou_line:
                        tot += M[i, j]
            r.append(tot - po)
        return r
    sol = least_squares(resid, [1.3, 1.1], bounds=([0.2, 0.2], [4.5, 4.5]), max_nfev=400)
    return float(sol.x[0]), float(sol.x[1])

def log_loss(p_dist: dict, actual: str) -> float:
    p = p_dist.get(actual, 1e-6)
    p = min(max(p, 1e-6), 1.0)
    return -math.log(p)

def brier(p_dist: dict, actual: str) -> float:
    s = 0.0
    for k, v in p_dist.items():
        s += (v - (1.0 if k == actual else 0.0)) ** 2
    # 未列出的比分实际未发生(赛果已知在列出项内), 补0即可
    return s

def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); cur = con.cursor()
    cur.execute("""
    SELECT p.home, p.away, p.kickoff, p.odds_json,
           m.op_1x2_h, m.op_1x2_d, m.op_1x2_a,
           m.op_ou_line, m.op_ou_over, m.op_ou_under,
           m.score_home, m.score_away
    FROM pre_match_cs p
    JOIN match_outcomes m ON p.home=m.home AND p.away=m.away
       AND date(p.kickoff)=date(m.kickoff)
    WHERE m.score_home IS NOT NULL AND m.score_away IS NOT NULL
      AND m.op_1x2_h IS NOT NULL AND m.op_ou_over IS NOT NULL
    """)
    rows = cur.fetchall(); con.close()
    out = []
    for r in rows:
        (home, away, ko, oj, oh, od, oa, ou_line, ou_o, ou_u, sh, sa) = r
        try:
            cs = json.loads(oj)
        except Exception:
            continue
        if not isinstance(cs, dict) or len(cs) < 5:
            continue
        out.append(dict(home=home, away=away, kickoff=ko,
                        cs=cs, oh=oh, od=od, oa=oa,
                        ou_line=ou_line, ou_o=ou_o, ou_u=ou_u,
                        score=f"{int(sh)}-{int(sa)}"))
    # sort by kickoff
    out.sort(key=lambda x: str(x["kickoff"]))
    return out

def empirical_prior(rows):
    """联赛经验比分频率 (训练集内)."""
    cnt = {}
    tot = 0
    for r in rows:
        k = grid_to_key(*map(int, r["score"].split("-")))
        cnt[k] = cnt.get(k, 0) + 1; tot += 1
    return {k: v / tot for k, v in cnt.items()}, tot

def main():
    rows = load()
    print(f"可用配对: {len(rows)}")
    n_train = int(len(rows) * 0.8)
    train, test = rows[:n_train], rows[n_train:]
    print(f"train={len(train)} test={len(test)}")

    prior, prior_n = empirical_prior(train)
    # 默认先验(平滑): 用训练频率, 测试时 unseen 给小值
    prior_vec = np.array([prior.get(grid_to_key(i, j), 1e-4) for i in range(MAXG + 1) for j in range(MAXG + 1)])
    prior_vec = prior_vec / prior_vec.sum()

    res = {"B": [], "M1": [], "M2": []}
    top1 = {"B": 0, "M1": 0, "M2": 0}
    n = {"B": 0, "M1": 0, "M2": 0}

    for r in test:
        cs = {k: float(v) for k, v in r["cs"].items()}
        book = demargin_cs(cs)
        # M1: 1X2 -> λ/μ
        ph, pd, pa = 1/r["oh"], 1/r["od"], 1/r["oa"]
        z = ph + pd + pa; ph, pd, pa = ph/z, pd/z, pa/z
        try:
            lh, la = fit_lambda_mu(ph, pd, pa)
        except Exception:
            continue
        M1 = poisson_marginal(lh, la)
        m1 = {grid_to_key(i, j): float(M1[i, j]) for i in range(MAXG + 1) for j in range(MAXG + 1)}
        # M2: + OU 约束 + shrinkage
        po = (1/r["ou_o"]) / (1/r["ou_o"] + 1/r["ou_u"])
        try:
            lh2, la2 = fit_lambda_mu(ph, pd, pa, po=po, ou_line=r["ou_line"] or 2.5)
        except Exception:
            lh2, la2 = lh, la
        M2 = poisson_marginal(lh2, la2)
        # shrinkage toward league prior
        alpha = 0.25
        M2s = (1 - alpha) * M2.flatten() + alpha * prior_vec
        M2s = M2s / M2s.sum()
        m2 = {grid_to_key(i, j): float(M2s[i * (MAXG + 1) + j]) for i in range(MAXG + 1) for j in range(MAXG + 1)}

        for name, dist in [("B", book), ("M1", m1), ("M2", m2)]:
            n[name] += 1
            res[name].append(log_loss(dist, r["score"]))
            if max(dist, key=dist.get) == r["score"]:
                top1[name] += 1

    print("\n=== 测试结果 (test set, 时间外推) ===")
    print(f"{'模型':<6}{'log_loss(↓)':>14}{'top1命中':>12}{'top1率':>10}")
    for name in ["B", "M1", "M2"]:
        ll = np.mean(res[name])
        t1 = top1[name] / n[name]
        print(f"{name:<6}{ll:>14.4f}{top1[name]:>10d}/{n[name]:<3}{t1:>9.1%}")
    # skill vs book
    lb = np.mean(res["B"])
    print("\n=== 相对庄家基线的技能分 (1 - LL_model/LL_book, >0=更好) ===")
    for name in ["M1", "M2"]:
        ll = np.mean(res[name])
        print(f"  {name}: skill={1 - ll/lb:+.3f}  (LL {ll:.4f} vs book {lb:.4f})")

if __name__ == "__main__":
    main()
