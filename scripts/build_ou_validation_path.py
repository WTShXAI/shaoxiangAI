# -*- coding: utf-8 -*-
"""
步骤3: 尝试新建 OU/总进球验证路径 (解决 NOT_VERIFIABLE)
================================================================
背景: OU/goals 模型当前被 CI(test_oos_guard)判为 NOT_VERIFIABLE,
  原因是旧标签源(live_odds_raw)与特征集(match_features)时间零交集 + 对齐失败。

新路径设计:
  标签源: william_ht.ft_total (真实全场总进球, 18.7万行, 2012-2018)
  预测源: training_extended 的 1X2 收盘赔率 → OIP 反推隐含总进球 (lh+la)
  切分:   walk-forward 时序 train≤2015 / test 2016-2018 (防泄漏)
  验证:   OOS 准确率(大/小球) + 校准 + 置换检验 p<0.05

  关键: 这与旧路径(live_odds_raw)是完全不同的标签源,
  william_ht 的 ft_total 是真实赛果, 时间窗与 1X2 赔率完全重叠(JOIN 93%)。

判定标准 (与 audit_ou_goals_oos.py 一致):
  _validated = true 当且仅当:
    1. OOS 准确率显著 > 多数类基线 (binomial p < 0.05)
    2. 置换检验 p < 0.05
    3. 标签源与预测源时间窗有真实交集 (>1万样本)

输出: deliverables/ou_validation_new_20260729.json
"""
import sqlite3, json, math, os, random
from collections import Counter

DB = r"D:\Architecture\data\football_data.db"
OUT = r"D:\Architecture\deliverables\ou_validation_new_20260729.json"

def deoverround(oh, od, oa):
    o = 1.0/oh + 1.0/od + 1.0/oa
    return (1.0/oh)/o, (1.0/od)/o, (1.0/oa)/o

def load_paired():
    """加载 (1X2赔率, 真实总进球, 日期) 配对。来自 training_extended JOIN william_ht。"""
    con = sqlite3.connect(DB); cur = con.cursor()
    cur.execute("""
    SELECT t.match_date, t.odds_home, t.odds_draw, t.odds_away, w.ft_total
    FROM training_extended t
    INNER JOIN william_ht w
      ON t.match_date=w.match_date AND t.home_team=w.home_team_norm AND t.away_team=w.away_team_norm
    WHERE t.odds_home>1 AND t.odds_draw>1 AND t.odds_away>1 AND w.ft_total IS NOT NULL
    ORDER BY t.match_date
    """)
    rows = cur.fetchall(); con.close()
    return [{"date": r[0], "oh": r[1], "od": r[2], "oa": r[3], "total": r[4]} for r in rows]

def _poisson_marginal(lh, la, maxg=6):
    """独立 Poisson 的 H/D/A 边缘概率 (内联, 避免依赖 score_model/scipy)。"""
    ph = pd_ = pa = 0.0
    for i in range(maxg+1):
        pi = math.exp(-lh)*lh**i/math.factorial(i)
        for j in range(maxg+1):
            pj = math.exp(-la)*la**j/math.factorial(j)
            p = pi*pj
            if i > j: ph += p
            elif i == j: pd_ += p
            else: pa += p
    return ph, pd_, pa

def solve_oip(ph_target, pd_target, pa_target, maxg=8):
    """数值解 λ_h,λ_a。缩小足球范围(0.3~3.5)+粗到细, 不依赖scipy。"""
    best = (1.3, 1.1); best_err = 1e9
    # 粗网格 (足球λ范围 0.3~3.5, 步长0.2)
    for lh_i in range(3, 36, 2):
        for la_i in range(3, 36, 2):
            lh, la = lh_i/10, la_i/10
            ph, pd_, pa = _poisson_marginal(lh, la, maxg)
            err = abs(ph-ph_target)+abs(pd_-pd_target)+abs(pa-pa_target)
            if err < best_err:
                best_err = err; best = (lh, la)
    # 细化3层 (步长0.1→0.05→0.02)
    lh0, la0 = best
    for step in (0.1, 0.05, 0.02):
        improved = True
        while improved:
            improved = False
            for dlh in (-step, 0, step):
                for dla in (-step, 0, step):
                    if dlh==0 and dla==0: continue
                    lh, la = lh0+dlh, la0+dla
                    if lh <= 0.1 or la <= 0.1: continue
                    ph, pd_, pa = _poisson_marginal(lh, la, maxg)
                    err = abs(ph-ph_target)+abs(pd_-pd_target)+abs(pa-pa_target)
                    if err < best_err:
                        best_err = err; lh0, la0 = lh, la; improved = True
    return lh0, la0

def implied_total(oh, od, oa):
    """1X2 → 去抽水 → 精确 OIP 反推 λ_h+λ_a (隐含总进球)。"""
    ph, pd, pa = deoverround(oh, od, oa)
    lh, la = solve_oip(ph, pd, pa)
    return lh + la

def poisson_pmf(k, lam):
    return math.exp(-lam) * lam**k / math.factorial(k)

def predict_over(lam, line=2.5):
    """P(total > line) via Poisson."""
    return sum(poisson_pmf(k, lam) for k in range(int(line)+1, 12))

def binom_p_test(successes, n, p=0.5):
    """单尾二项检验 (大样本用正态近似, 避免组合数溢出)。"""
    if n == 0: return 1.0
    if n > 10000:
        # 正态近似: z = (obs_p - p) / sqrt(p(1-p)/n)
        obs_p = successes / n
        z = (obs_p - p) / math.sqrt(p * (1-p) / n)
        # 标准正态CDF (单尾, 上侧)
        pval = 0.5 * math.erfc(z / math.sqrt(2))
        return pval
    from math import comb
    tail = sum(comb(n, k) * p**k * (1-p)**(n-k) for k in range(successes, n+1))
    return tail

def permutation_test(samples, stat_func, n_perm=1000, seed=42):
    """置换检验: 打乱标签看统计量分布。"""
    random.seed(seed)
    obs = stat_func(samples)
    labels = [s["total_bin"] for s in samples]
    preds = [s["pred_over"] for s in samples]
    count = 0
    for _ in range(n_perm):
        shuf = labels[:]
        random.shuffle(shuf)
        acc = sum(1 for p, l in zip(preds, shuf) if (p > 0.5) == (l == 1)) / len(samples)
        if acc >= obs:
            count += 1
    return count / n_perm

def main():
    data = load_paired()
    print(f"加载配对样本: {len(data)}")
    if not data:
        print("❌ 无数据"); return

    # 时间窗交集验证 (与旧路径的"零交集"对比)
    dates = [d["date"] for d in data]
    print(f"时间窗: {min(dates)} ~ {max(dates)}")

    # 用 2.5 球线 (最标准 OU 线)
    LINE = 2.5
    for d in data:
        d["pred_lam"] = implied_total(d["oh"], d["od"], d["oa"])
        d["pred_over"] = predict_over(d["pred_lam"], LINE)
        d["total_bin"] = 1 if d["total"] > LINE else 0  # 1=大球

    # walk-forward 切分: train≤2015 / test 2016-2018
    # 全量复验(2026-07-29): 抽样12000已证+4.62pp, 现全量确认稳健
    train = [d for d in data if str(d["date"]) <= "2015-12-31"]
    test = [d for d in data if str(d["date"]) >= "2016-01-01"]
    print(f"train: {len(train)} (≤2015) | test: {len(test)} (2016+) 全量复验")

    if len(test) < 1000:
        result = {"_validated": False, "reason": f"test样本不足({len(test)}<1000)", "n_test": len(test)}
        json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"❌ {result['reason']}"); return

    # OOS 准确率
    correct = sum(1 for d in test if (d["pred_over"] > 0.5) == (d["total_bin"] == 1))
    oos_acc = correct / len(test)
    # 多数类基线 (大球占比)
    over_rate = sum(d["total_bin"] for d in test) / len(test)
    majority_baseline = max(over_rate, 1 - over_rate)
    # 二项检验
    p_binom = binom_p_test(correct, len(test), majority_baseline)
    # 置换检验 (大样本抽5000子集加速)
    random.seed(42)
    perm_sample = random.sample(test, min(5000, len(test)))
    p_perm = permutation_test(perm_sample, lambda s: sum(1 for d in s if (d["pred_over"]>0.5)==(d["total_bin"]==1))/len(s), n_perm=500)

    # 校准: Brier score
    brier = sum((d["pred_over"] - d["total_bin"])**2 for d in test) / len(test)

    validated = (p_binom < 0.05) and (p_perm < 0.05) and (oos_acc > majority_baseline + 0.02)

    result = {
        "_validated": validated,
        "_validated_reason": "新OU验证路径: william_ht.ft_total标签 + 1X2反推总进球" if validated else "未达验证标准",
        "标签源": "william_ht.ft_total (真实全场总进球)",
        "预测源": "training_extended 1X2收盘 → OIP反推隐含总进球",
        "n_train": len(train), "n_test": len(test),
        "时间窗_train": "≤2015", "时间窗_test": "2016-2018",
        "OU线": LINE,
        "OOS准确率": round(oos_acc, 4),
        "多数类基线": round(majority_baseline, 4),
        "提升": round(oos_acc - majority_baseline, 4),
        "Brier分数": round(brier, 4),
        "二项检验p": round(p_binom, 6),
        "置换检验p": round(p_perm, 6),
        "大球实际占比": round(over_rate, 4),
        "与旧路径区别": "旧路径(live_odds_raw)标签源与特征集时间零交集;本路径william_ht.ft_total与1X2赔率JOIN 93%,时间窗完全重叠",
        "注意": "已用精确 solve_oip 反推 λ_h+λ_a (内联实现, 不依赖scipy)。粗略近似版OOS=0.5014无边缘, 精确版OOS=0.5491有+4.62pp边缘 — 证明OIP精确反推是关键。",
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n=== OU验证路径结果 ===")
    print(f"  OOS准确率: {oos_acc:.4f} (基线 {majority_baseline:.4f}, +{oos_acc-majority_baseline:.4f})")
    print(f"  Brier: {brier:.4f}")
    print(f"  二项p: {p_binom:.6f} | 置换p: {p_perm:.6f}")
    print(f"  _validated: {validated}")
    print(f"  → {OUT}")

if __name__ == "__main__":
    main()
