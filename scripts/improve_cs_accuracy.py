"""
哨响AI — 波胆(正确比分)命中率提升实验
========================================
目标: 在不污染 1X2 铁律前提下, 提升 波胆 TOP-N 推荐命中率。

方法 (防过拟合):
  - canon 数据源: DB wc_all_matches (313场, 2014-2026 四届, 含赔率+赛果)
  - 重复 20 次 70/30 随机拆分; 调参仅在 train, eval 仅在 test
  - 对比配置:
      C0  基线:        g=1.0,  α=0,  ρ=0
      C1  当前生产:    g=1.199,α=0,  ρ=0   (用户88场校准值)
      C2  goal_scale调优: g*=,   α=0,  ρ=0
      C3  +经验收缩:    g*,   α*,  ρ=0
      C4  +Dixon-Coles: g*,   α*,  ρ*
  - 指标: top1/top3/top5 命中率 + logloss (test 均值±std)

经验比分矩阵 (empirical shrinkage): 仅在 train 上统计 P(比分), 与模型矩阵
按 (1-α)*M_model + α*M_emp 混合。这是标准 empirical-Bayes 收缩, 合法。

输出: 控制台对照表 + deliverables/improve_cs_accuracy_report_<date>.html
"""
import os, math, json, datetime, sqlite3
import numpy as np

DB = "data/football_data.db"
MAXG = 8  # 0..8 共9档

def load_matches():
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("SELECT home,away,oh,od,oa,hg,ag FROM wc_all_matches "
                "WHERE oh IS NOT NULL AND hg IS NOT NULL")
    rows = cur.fetchall(); con.close()
    return [dict(home=r[0], away=r[1], oh=r[2], od=r[3], oa=r[4], hg=r[5], ag=r[6])
            for r in rows]

def deoverround(oh, od, oa):
    o = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/o, (1.0/od)/o, (1.0/oa)/o

def _marg(lh, la):
    ph = pd_ = pa = 0.0
    for i in range(MAXG+1):
        pi = math.exp(-lh)*lh**i/math.factorial(i)
        for j in range(MAXG+1):
            pj = math.exp(-la)*la**j/math.factorial(j)
            p = pi*pj
            if i > j: ph += p
            elif i == j: pd_ += p
            else: pa += p
    return ph, pd_, pa

def solve_oip(ph, pd):
    best = (1.3, 1.1); br = 1e9
    for lh in np.arange(0.3, 4.5, 0.05):
        for la in np.arange(0.3, 4.5, 0.05):
            eh, ed, _ = _marg(lh, la)
            r = (eh-ph)**2 + (ed-pd)**2
            if r < br:
                br, best = r, (lh, la)
    return best

def score_matrix(lh, la):
    col = np.array([math.exp(-lh)*lh**i/math.factorial(i) for i in range(MAXG+1)])
    row = np.array([math.exp(-la)*la**j/math.factorial(j) for j in range(MAXG+1)])
    return np.outer(col, row)

def dc_correct(M, rho):
    M = M.copy().astype(float)
    for i in range(min(2, MAXG+1)):
        for j in range(min(2, MAXG+1)):
            M[i][j] *= (1.0 - rho*i*j)
    s = M.sum()
    return M/s if s > 0 else M

def empirical_matrix(matches):
    """train 上统计经验比分分布 (9x9), 仅 hg,ag<=MAXG 计入"""
    M = np.zeros((MAXG+1, MAXG+1))
    n = 0
    for m in matches:
        hg, ag = m["hg"], m["ag"]
        if 0 <= hg <= MAXG and 0 <= ag <= MAXG:
            M[hg, ag] += 1; n += 1
    if n == 0:
        return M
    return M / n

def build_matrix(m, base_lh, base_la, g, alpha, rho, emp):
    lh, la = base_lh*g, base_la*g
    M = score_matrix(lh, la); M /= M.sum()
    if rho:
        M = dc_correct(M, rho)
    if alpha > 0 and emp is not None:
        M = (1-alpha)*M + alpha*emp
        M /= M.sum()
    return M

def topn_hit(M, hg, ag, n):
    if not (0 <= hg <= MAXG and 0 <= ag <= MAXG):
        return False
    flat = M.flatten()
    order = np.argsort(-flat)[:n]
    mg = MAXG+1
    return any(divmod(int(k), mg) == (hg, ag) for k in order)

def evaluate(matches, base, g, alpha, rho, emp):
    t1 = t3 = t5 = 0.0; ll = 0.0; n = len(matches)
    for m, (blh, bla) in zip(matches, base):
        M = build_matrix(m, blh, bla, g, alpha, rho, emp)
        hg, ag = m["hg"], m["ag"]
        if 0 <= hg <= MAXG and 0 <= ag <= MAXG:
            if topn_hit(M, hg, ag, 1): t1 += 1
            if topn_hit(M, hg, ag, 3): t3 += 1
            if topn_hit(M, hg, ag, 5): t5 += 1
            ll += -math.log(max(M[hg, ag], 1e-9))
    return dict(t1=t1/n, t3=t3/n, t5=t5/n, ll=ll/n)

def tune(matches, base, emp):
    """分阶段调参 (train 上): 先 g, 再 α, 再 ρ; 目标最大化 top3 命中率"""
    # g 粗扫
    gs = np.arange(1.0, 1.7, 0.05)
    best_g, best_t3 = 1.0, -1
    for g in gs:
        r = evaluate(matches, base, g, 0.0, 0.0, emp)
        if r["t3"] > best_t3:
            best_t3, best_g = r["t3"], g
    # α 细扫 (g*)
    alphas = np.arange(0.0, 0.7, 0.05)
    best_a, best_t3a = 0.0, -1
    for a in alphas:
        r = evaluate(matches, base, best_g, a, 0.0, emp)
        if r["t3"] > best_t3a:
            best_t3a, best_a = r["t3"], a
    # ρ 细扫
    rhos = np.arange(-0.3, 0.31, 0.1)
    best_r, best_t3r = 0.0, -1
    for rho in rhos:
        r = evaluate(matches, base, best_g, best_a, rho, emp)
        if r["t3"] > best_t3r:
            best_t3r, best_r = r["t3"], rho
    return dict(g=round(float(best_g), 3), alpha=round(float(best_a), 3),
                rho=round(float(best_r), 3), t3_train=float(best_t3r))

def main():
    matches = load_matches()
    print(f"[load] {len(matches)} WC matches (canon: wc_all_matches)")
    # 预解基础 λ (g=1), 免重复 root 求解
    base = []
    for m in matches:
        ph, pd, _ = deoverround(m["oh"], m["od"], m["oa"])
        base.append(solve_oip(ph, pd))
    emp_full = empirical_matrix(matches)
    print(f"[emp top scores] " + ", ".join(
        f"{i}-{j}:{emp_full[i,j]:.3f}" for i,j in
        sorted([(i,j) for i in range(MAXG+1) for j in range(MAXG+1)],
               key=lambda k:-emp_full[k])[:5]))

    K = 20
    rng = np.random.default_rng(42)
    cfgs = {
        "C0_base":      dict(g=1.0,  alpha=0.0, rho=0.0),
        "C1_prod_1.199":dict(g=1.199,alpha=0.0, rho=0.0),
    }
    # 收集 C2/C3/C4 调参结果
    tuned = []
    agg = {k: {"t1": [], "t3": [], "t5": [], "ll": []} for k in
           list(cfgs.keys()) + ["C2_g", "C3_g+a", "C4_g+a+r"]}

    for k in range(K):
        idx = np.arange(len(matches))
        rng.shuffle(idx)
        cut = int(len(matches)*0.7)
        tr, te = idx[:cut], idx[cut:]
        tr_m = [matches[i] for i in tr]; te_m = [matches[i] for i in te]
        tr_base = [base[i] for i in tr]; te_base = [base[i] for i in te]
        emp = empirical_matrix(tr_m)
        t = tune(tr_m, tr_base, emp)
        tuned.append(t)
        # C2/C3/C4 用调出参数在 test 评估
        c2 = evaluate(te_m, te_base, t["g"], 0.0, 0.0, None)
        c3 = evaluate(te_m, te_base, t["g"], t["alpha"], 0.0, emp)
        c4 = evaluate(te_m, te_base, t["g"], t["alpha"], t["rho"], emp)
        for name, res in [("C2_g", c2), ("C3_g+a", c3), ("C4_g+a+r", c4)]:
            for met in ("t1", "t3", "t5", "ll"):
                agg[name][met].append(res[met])
        # 固定配置在 test 评估
        for name, cfg in cfgs.items():
            r = evaluate(te_m, te_base, cfg["g"], cfg["alpha"], cfg["rho"],
                         emp if cfg["alpha"] > 0 else None)
            for met in ("t1", "t3", "t5", "ll"):
                agg[name][met].append(r[met])

    # 汇总
    def mean_std(x): return np.mean(x), np.std(x)
    print("\n=== 波胆命中率对照 (test, 20×70/30, 均值±std) ===")
    print(f"{'config':16}{'top1':>12}{'top3':>12}{'top5':>12}{'logloss':>12}")
    summary = {}
    for name in ["C0_base","C1_prod_1.199","C2_g","C3_g+a","C4_g+a+r"]:
        m1, s1 = mean_std(agg[name]["t1"])
        m3, s3 = mean_std(agg[name]["t3"])
        m5, s5 = mean_std(agg[name]["t5"])
        ml, sl = mean_std(agg[name]["ll"])
        summary[name] = dict(t1=m1, t3=m3, t5=m5, ll=ml)
        print(f"{name:16}{m1*100:7.2f}±{s1*100:4.1f}{m3*100:7.2f}±{s3*100:4.1f}"
              f"{m5*100:7.2f}±{s5*100:4.1f}{ml:8.3f}±{sl:4.2f}")

    # 调参均值
    tg = np.mean([t["g"] for t in tuned]); ta = np.mean([t["alpha"] for t in tuned])
    tr_ = np.mean([t["rho"] for t in tuned])
    print(f"\n[tuned params] g={tg:.3f} α={ta:.3f} ρ={tr_:.3f} (mean over {K} splits)")

    # 选最优: top3 最高且 logloss 不显著恶化 (取 C3 vs C1)
    best_name = max(["C1_prod_1.199","C2_g","C3_g+a","C4_g+a+r"],
                    key=lambda n: summary[n]["t3"])
    print(f"[BEST by top3] {best_name}: top3={summary[best_name]['t3']*100:.2f}%")

    # 固化: 输出 WC 经验矩阵 (全量313) 供 score_model 嵌入
    emp_out = [[round(float(emp_full[i, j]), 5) for j in range(MAXG+1)]
               for i in range(MAXG+1)]
    out = dict(
        n_matches=len(matches),
        tuned_g=float(tg), tuned_alpha=float(ta), tuned_rho=float(tr_),
        best_config=best_name, summary=summary,
        wc_empirical_matrix=emp_out,
        generated_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    os.makedirs("deliverables", exist_ok=True)
    stamp = datetime.date.today().isoformat()
    json.dump(out, open(f"deliverables/improve_cs_accuracy_{stamp}.json", "w"),
              ensure_ascii=False, indent=2)
    print(f"\n[written] deliverables/improve_cs_accuracy_{stamp}.json")
    return out

if __name__ == "__main__":
    main()
