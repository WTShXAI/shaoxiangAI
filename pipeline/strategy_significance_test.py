"""
strategy_significance_test.py — HIGH risk 小球策略的统计显著性检验

背景:
  backtest_high_risk_under.py 报出:
    HIGH risk 全小球 ROI +12.32% (n=50)
    Youth/U 级 ROI +28.89% (n=28)
  n=50 / n=28 是极小样本, 且我们在多个维度上分层搜索过 (risk / 赛事类型 / OU线),
  存在严重的 multiple-testing 与数据窥探(data snooping)风险。

  铁律: 小样本规则主导 + 命中率必须并排 naive 基线。
  故: 在把策略接入生产/前端之前, 必须过四道关。

四道关:
  1. Bootstrap 95% CI  : ROI 的置信下界是否 > 0
  2. 置换检验          : 打乱 risk 标签 10000 次, 真实 ROI 的 p 值
  3. 时间切分 OOS      : 前 60% 选策略, 后 40% 验证 (无泄漏)
  4. 多重检验修正      : 对同时测试的 K 个分层做 Bonferroni / White Reality Check

输出: data/pricing_template/strategy_significance_report.json
"""
import os
import sys
import json
import logging
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.clean_outcomes import load_clean_outcomes  # noqa: E402
from pipeline.opening_line import build_opening_lines    # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
GQ_DB = os.path.join(DATA_DIR, "events.db")
OUT_DIR = os.path.join(DATA_DIR, "pricing_template")

RNG = np.random.default_rng(20260805)
N_BOOT = 10000
N_PERM = 10000


# ---------------------------------------------------------------- 数据
def load_bets() -> pd.DataFrame:
    """构造 全量可下注池: 每场一注小球, 记录 profit + risk 分数 + 分类."""
    scan = pd.read_csv(os.path.join(OUT_DIR, "template_deviation_scan.csv"))
    # 2026-08-05: 必须走 clean_outcomes —— 直接读 match_outcomes 会吃进
    # 228 场电子盘 + 1535 场采集截断(比分被冻结), 小球策略会凭空"盈利"。
    oc, rep = load_clean_outcomes(return_report=True)
    log.info(f"[数据] 清洗前 n={rep['before']['n']} 均进球={rep['before']['avg_goals']} "
             f"零封={rep['before']['zero_rate']:.2%}  ->  "
             f"清洗后 n={rep['after']['n']} 均进球={rep['after']['avg_goals']} "
             f"零封={rep['after']['zero_rate']:.2%} "
             f"(剔虚拟{rep['dropped_virtual']} 剔截断{rep['dropped_truncated']})")
    # 2026-08-05 二次修复: match_outcomes.op_ou_line 是"梯队最小线"不是主盘线,
    # 会把大球做成必热(全池盲投大球 +15% 假 edge)。改用 opening_line 重建的主盘。
    op = build_opening_lines()
    oc["match_key"] = oc["home"].astype(str) + " vs " + oc["away"].astype(str)
    oc = oc.drop(columns=[c for c in ("op_ou_line", "op_ou_over", "op_ou_under")
                          if c in oc.columns])
    oc = oc.merge(op[["match_key", "line", "over", "under", "overround"]],
                  on="match_key", how="inner").drop_duplicates(subset=["mid"])
    oc = oc.rename(columns={"line": "op_ou_line", "over": "op_ou_over",
                            "under": "op_ou_under"})
    log.info(f"[数据] 初盘主盘线匹配 {len(oc)} 场, 均线位={oc['op_ou_line'].mean():.3f} "
             f"中位={oc['op_ou_line'].median():.2f} 均抽水={oc['overround'].mean():.2%}")
    oc = oc[["mid", "score_home", "score_away",
             "op_ou_line", "op_ou_over", "op_ou_under"]]
    oc["mid"] = oc["mid"].astype(str)
    scan["mid"] = scan["mid"].astype(str)
    df = scan.merge(oc, on="mid", how="inner")
    df = df[df["op_ou_line"].notna() & df["op_ou_under"].notna()].copy()
    df["total_goals"] = df["score_home"] + df["score_away"]
    df["date"] = pd.to_datetime(df["kickoff"], errors="coerce")

    line = df["op_ou_line"].astype(float)
    tot = df["total_goals"].astype(float)
    und = df["op_ou_under"].astype(float)
    df["profit"] = np.where(tot < line, und - 1.0, np.where(tot > line, -1.0, 0.0))
    df["risk"] = df["template_risk_score"].astype(float)

    # 赛事分类 (与回测一致)
    lg = df["league"].fillna("").astype(str)
    df["cat"] = np.select(
        [
            lg.str.contains("U1[6-9]|U2[0-3]|青年|预备|Youth|Reserve", regex=True, na=False),
            lg.str.contains("友谊|Friendly", regex=True, na=False),
            lg.str.contains("杯|Cup", regex=True, na=False),
        ],
        ["Youth", "Friendly", "Cup"],
        default="Other",
    )
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------- 统计工具
def bootstrap_ci(profits: np.ndarray, n_boot: int = N_BOOT, alpha: float = 0.05):
    """ROI 的 bootstrap 百分位 CI."""
    n = len(profits)
    if n < 5:
        return None
    idx = RNG.integers(0, n, size=(n_boot, n))
    rois = profits[idx].mean(axis=1)
    lo, hi = np.percentile(rois, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {
        "roi": round(float(profits.mean()), 4),
        "ci_lo": round(float(lo), 4),
        "ci_hi": round(float(hi), 4),
        "p_roi_gt_0": round(float((rois > 0).mean()), 4),
    }


def permutation_test(df: pd.DataFrame, mask: np.ndarray, n_perm: int = N_PERM):
    """
    H0: 被选中的子集与随机抽取同样大小的子集无差异。
    随机重排「谁被选中」n_perm 次, 统计真实 ROI 的分位。
    """
    profits = df["profit"].values
    n_sel = int(mask.sum())
    if n_sel < 5 or n_sel >= len(profits):
        return None
    real_roi = float(profits[mask].mean())
    null_rois = np.empty(n_perm)
    N = len(profits)
    for i in range(n_perm):
        pick = RNG.choice(N, size=n_sel, replace=False)
        null_rois[i] = profits[pick].mean()
    p_one_sided = float((null_rois >= real_roi).mean())
    return {
        "real_roi": round(real_roi, 4),
        "null_roi_mean": round(float(null_rois.mean()), 4),
        "null_roi_std": round(float(null_rois.std()), 4),
        "p_value": round(p_one_sided, 4),
        "z": round(float((real_roi - null_rois.mean()) / max(1e-9, null_rois.std())), 3),
    }


def summarize(profits: np.ndarray) -> dict:
    n = len(profits)
    w = int((profits > 0).sum())
    l = int((profits < 0).sum())
    p = int((profits == 0).sum())
    return {
        "n": n, "win": w, "loss": l, "push": p,
        "hit_rate": round(w / max(1, w + l), 4),
        "roi": round(float(profits.mean()), 4) if n else 0.0,
        "total_profit": round(float(profits.sum()), 2),
    }


# ---------------------------------------------------------------- 主检验
def main():
    df = load_bets()
    log.info("=" * 66)
    log.info("HIGH risk 小球策略 — 统计显著性检验")
    log.info("=" * 66)
    log.info(f"可下注池: {len(df)} 场 ({df['date'].min().date()} ~ {df['date'].max().date()})")

    base = summarize(df["profit"].values)
    log.info(f"\n[基准] 全池全小球: n={base['n']}, 命中={base['hit_rate']:.1%}, ROI={base['roi']:+.2%}")

    # ---- 待检策略集合 (这些就是我们"搜索过"的假设, 必须一起做多重检验修正)
    strategies = {
        "HIGH(risk>0.6)": df["risk"] > 0.6,
        "MEDIUM+(risk>0.5)": df["risk"] > 0.5,
        "Youth": df["cat"] == "Youth",
        "Friendly": df["cat"] == "Friendly",
        "Cup": df["cat"] == "Cup",
        "HIGH+Youth": (df["risk"] > 0.6) & (df["cat"] == "Youth"),
        "HIGH+OU<=2.5": (df["risk"] > 0.6) & (df["op_ou_line"].astype(float) <= 2.5),
    }

    log.info("\n" + "-" * 66)
    log.info("关卡 1+2: Bootstrap 95% CI  &  置换检验")
    log.info("-" * 66)
    log.info(f"{'策略':<20s}{'n':>5s}{'命中':>8s}{'ROI':>9s}{'CI下界':>10s}{'CI上界':>10s}{'p值':>8s}")
    log.info("-" * 66)

    rows = []
    for name, mask in strategies.items():
        m = mask.values if hasattr(mask, "values") else mask
        pr = df.loc[m, "profit"].values
        if len(pr) < 5:
            continue
        s = summarize(pr)
        ci = bootstrap_ci(pr)
        pt = permutation_test(df, m)
        rows.append({
            "strategy": name, **s,
            "bootstrap": ci, "permutation": pt,
        })
        log.info(f"{name:<20s}{s['n']:>5d}{s['hit_rate']:>8.1%}{s['roi']:>+9.2%}"
                 f"{ci['ci_lo']:>+10.2%}{ci['ci_hi']:>+10.2%}"
                 f"{(pt['p_value'] if pt else float('nan')):>8.4f}")

    # ---- 关卡 4: 多重检验修正
    K = len(rows)
    alpha = 0.05
    bonf = alpha / K
    log.info("\n" + "-" * 66)
    log.info(f"关卡 4: 多重检验修正 (同时测了 K={K} 个策略)")
    log.info(f"  Bonferroni 阈值: alpha/K = {bonf:.4f}")
    log.info("-" * 66)
    survivors = []
    for r in rows:
        p = r["permutation"]["p_value"] if r["permutation"] else 1.0
        ci_lo = r["bootstrap"]["ci_lo"] if r["bootstrap"] else -1
        ok_p = p < bonf
        ok_ci = ci_lo > 0
        r["passes_bonferroni"] = bool(ok_p)
        r["ci_lo_positive"] = bool(ok_ci)
        r["survives"] = bool(ok_p and ok_ci)
        flag = "存活" if r["survives"] else ("边缘" if (ok_p or ok_ci) else "淘汰")
        log.info(f"  {r['strategy']:<20s} p={p:.4f} {'<' if ok_p else '>='}{bonf:.4f}  "
                 f"CI下界={ci_lo:+.2%}  -> {flag}")
        if r["survives"]:
            survivors.append(r["strategy"])

    # ---- 关卡 3: 时间切分 OOS
    log.info("\n" + "-" * 66)
    log.info("关卡 3: 时间切分 OOS (前60%选策略 / 后40%验证, 无泄漏)")
    log.info("-" * 66)
    cut = int(len(df) * 0.6)
    tr, te = df.iloc[:cut], df.iloc[cut:]
    log.info(f"  IS: {len(tr)} 场 ({tr['date'].min().date()}~{tr['date'].max().date()})  "
             f"OOS: {len(te)} 场 ({te['date'].min().date()}~{te['date'].max().date()})")
    log.info(f"\n  {'策略':<20s}{'IS n':>6s}{'IS ROI':>10s}{'OOS n':>7s}{'OOS ROI':>10s}{'OOS CI下界':>12s}")
    log.info("  " + "-" * 62)

    oos_rows = []
    for name, mask in strategies.items():
        m = mask.values if hasattr(mask, "values") else mask
        m_tr, m_te = m[:cut], m[cut:]
        pr_tr = tr.loc[m_tr, "profit"].values
        pr_te = te.loc[m_te, "profit"].values
        if len(pr_tr) < 5 or len(pr_te) < 5:
            log.info(f"  {name:<20s}{len(pr_tr):>6d}{'--':>10s}{len(pr_te):>7d}{'样本不足':>10s}")
            oos_rows.append({"strategy": name, "is_n": len(pr_tr), "oos_n": len(pr_te),
                             "insufficient": True})
            continue
        ci_te = bootstrap_ci(pr_te)
        oos_rows.append({
            "strategy": name,
            "is_n": len(pr_tr), "is_roi": round(float(pr_tr.mean()), 4),
            "oos_n": len(pr_te), "oos_roi": round(float(pr_te.mean()), 4),
            "oos_ci_lo": ci_te["ci_lo"], "oos_ci_hi": ci_te["ci_hi"],
            "consistent": bool(pr_tr.mean() > 0 and pr_te.mean() > 0),
        })
        log.info(f"  {name:<20s}{len(pr_tr):>6d}{pr_tr.mean():>+10.2%}"
                 f"{len(pr_te):>7d}{pr_te.mean():>+10.2%}{ci_te['ci_lo']:>+12.2%}")

    # ---- 最终判定
    log.info("\n" + "=" * 66)
    log.info("最终判定")
    log.info("=" * 66)

    final = []
    for r in rows:
        o = next((x for x in oos_rows if x["strategy"] == r["strategy"]), {})
        oos_ok = bool(o.get("consistent")) and o.get("oos_ci_lo", -1) > 0
        verdict = "生产可用" if (r["survives"] and oos_ok) else \
                  "仅观察" if (r["survives"] or oos_ok) else "不可用"
        final.append({
            "strategy": r["strategy"], "n": r["n"], "roi": r["roi"],
            "hit_rate": r["hit_rate"],
            "ci_lo": r["bootstrap"]["ci_lo"], "ci_hi": r["bootstrap"]["ci_hi"],
            "perm_p": r["permutation"]["p_value"] if r["permutation"] else None,
            "bonferroni_pass": r["passes_bonferroni"],
            "oos_n": o.get("oos_n"), "oos_roi": o.get("oos_roi"),
            "oos_ci_lo": o.get("oos_ci_lo"),
            "verdict": verdict,
        })
        log.info(f"  {r['strategy']:<20s} n={r['n']:>4d} ROI={r['roi']:>+7.2%} "
                 f"CI=[{r['bootstrap']['ci_lo']:+.1%},{r['bootstrap']['ci_hi']:+.1%}] "
                 f"-> {verdict}")

    usable = [f["strategy"] for f in final if f["verdict"] == "生产可用"]
    watch = [f["strategy"] for f in final if f["verdict"] == "仅观察"]
    log.info(f"\n  生产可用: {usable if usable else '无'}")
    log.info(f"  仅观察  : {watch if watch else '无'}")
    if not usable:
        log.info("\n  [结论] 无策略通过全部四关 -> 前端只做「信息标注」, 不给下注建议。")

    out = {
        "baseline_all_under": base,
        "n_strategies_tested": K,
        "bonferroni_alpha": round(bonf, 5),
        "detail": rows,
        "oos": oos_rows,
        "final": final,
        "usable": usable,
        "watch_only": watch,
    }
    path = os.path.join(OUT_DIR, "strategy_significance_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    log.info(f"\n报告已存: {path}")
    return out


if __name__ == "__main__":
    main()
